#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Escáner de segmentos sin nombre en Waze (México).
Consulta la API pública del Waze Map Editor (la misma que usa el modo
práctica, sin necesidad de iniciar sesión), recorre el país en una malla
de celdas y guarda los segmentos sin nombre en docs/data/ para que la
página web (GitHub Pages) los muestre.

Uso:
  python scanner/scan.py --modo test               # zona de prueba (Guadalajara centro)
  python scanner/scan.py --modo completo --minutos 240
"""
import argparse
import json
import math
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("Falta la librería 'requests' (pip install requests)", file=sys.stderr)
    sys.exit(1)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE, "scanner", "config.json")
GEOJSON_PATH = os.path.join(BASE, "scanner", "mx_estados.geojson")
STATE_PATH = os.path.join(BASE, "state", "scan_state.json")
LASTRUN_PATH = os.path.join(BASE, "state", "last_run.json")
DEBUG_PATH = os.path.join(BASE, "state", "debug_ultimo_error.txt")
DATA_DIR = os.path.join(BASE, "docs", "data")
ESTADOS_DIR = os.path.join(DATA_DIR, "estados")

# Servidores del WME. México vive en el entorno "row" (Rest of World).
# El parámetro sandbox=true es el que usa el modo práctica: permite leer
# los datos del mapa sin iniciar sesión.
ENDPOINTS = {
    "row": "https://www.waze.com/row-Descartes/app/Features",
    "usa": "https://www.waze.com/Descartes/app/Features",
}
ORDEN_ENTORNOS = ("row", "usa")
EDITOR_PAGES = {
    "usa": "https://www.waze.com/editor?env=usa",
    "row": "https://www.waze.com/editor?env=row",
}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
    "Referer": "https://www.waze.com/editor",
    "X-Requested-With": "XMLHttpRequest",
}

# Tipos de vía que DEBEN tener nombre (los demás pueden ir sin nombre):
# 1 Calle, 2 Avenida principal, 3 Autopista, 6 Carretera mayor, 7 Carretera menor
ROAD_TYPE_NAMES = {
    1: "Calle", 2: "Avenida principal", 3: "Autopista", 4: "Rampa",
    5: "Sendero peatonal", 6: "Carretera mayor", 7: "Carretera menor",
    8: "Terracería", 10: "Andador", 15: "Ferry", 16: "Escaleras",
    17: "Camino privado", 18: "Vía de tren", 19: "Pista de aterrizaje",
    20: "Camino de estacionamiento", 22: "Callejón",
}

BBOX_PRUEBA = [-103.38, 20.655, -103.33, 20.695]  # Guadalajara centro


# ---------------------------------------------------------------- utilidades
def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "sin-estado"


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path, data, compact=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if compact:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(data, f, ensure_ascii=False, indent=1)


# ------------------------------------------------- estados (punto en polígono)
class EstadosMX:
    """Determina el estado mexicano de una coordenada (ray casting)."""

    def __init__(self, geojson_path):
        gj = load_json(geojson_path, {"features": []})
        self.features = []
        for f in gj.get("features", []):
            name = f.get("properties", {}).get("name", "¿?")
            geom = f.get("geometry", {})
            polys = []
            if geom.get("type") == "Polygon":
                polys = [geom["coordinates"]]
            elif geom.get("type") == "MultiPolygon":
                polys = geom["coordinates"]
            rings = []
            for poly in polys:
                if poly:
                    ring = poly[0]  # anillo exterior
                    xs = [p[0] for p in ring]
                    ys = [p[1] for p in ring]
                    rings.append((min(xs), min(ys), max(xs), max(ys), ring))
            self.features.append((name, rings))
        self._cache = {}

    @staticmethod
    def _inside(lon, lat, ring):
        n = len(ring)
        inside = False
        j = n - 1
        for i in range(n):
            xi, yi = ring[i][0], ring[i][1]
            xj, yj = ring[j][0], ring[j][1]
            if (yi > lat) != (yj > lat):
                x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
                if lon < x_cross:
                    inside = not inside
            j = i
        return inside

    def estado_de(self, lon, lat):
        key = (round(lon, 2), round(lat, 2))
        if key in self._cache:
            return self._cache[key]
        found = None
        for name, rings in self.features:
            for (minx, miny, maxx, maxy, ring) in rings:
                if minx <= lon <= maxx and miny <= lat <= maxy and self._inside(lon, lat, ring):
                    found = name
                    break
            if found:
                break
        if not found:
            # punto fuera de todo polígono (costa, frontera): el más cercano por bbox
            best, bestd = None, 1e9
            for name, rings in self.features:
                for (minx, miny, maxx, maxy, _r) in rings:
                    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
                    d = (cx - lon) ** 2 + (cy - lat) ** 2
                    if d < bestd:
                        bestd, best = d, name
            found = best or "Sin estado"
        self._cache[key] = found
        return found


# ------------------------------------------------------------------ API Waze
class AuthError(Exception):
    pass


class AreaError(Exception):
    pass


def nueva_sesion(env):
    s = requests.Session()
    s.headers.update(HEADERS)
    try:  # visita la página del editor (modo práctica) para obtener cookies
        s.get(EDITOR_PAGES[env], timeout=30)
    except requests.RequestException:
        pass
    return s


def _objetos(data, clave):
    v = data.get(clave)
    if isinstance(v, dict):
        v = v.get("objects", [])
    if not isinstance(v, list):
        return []
    out = []
    for o in v:
        if isinstance(o, dict) and "attributes" in o and isinstance(o["attributes"], dict):
            merged = dict(o["attributes"])
            merged.setdefault("id", o.get("id"))
            out.append(merged)
        else:
            out.append(o)
    return out


def pedir_celda(sesion, env, bbox, pausa, tipos=None):
    """Pide los features de un bbox. Devuelve dict con segments/streets/cities."""
    solicitados = sorted(set(tipos or [1, 2, 3, 6, 7]) | {4})
    params = {
        "bbox": f"{bbox[0]:.6f},{bbox[1]:.6f},{bbox[2]:.6f},{bbox[3]:.6f}",
        "language": "es",
        "v": "2",
        "apiV2": "true",
        "roadTypes": ",".join(str(t) for t in solicitados),
        "zoomLevel": "17",
        "sandbox": "true",  # el truco del modo práctica: lectura sin login
    }
    time.sleep(pausa)
    r = sesion.get(ENDPOINTS[env], params=params, timeout=60)
    texto = r.text[:2000]
    if r.status_code in (401, 403):
        raise AuthError(f"HTTP {r.status_code}: {texto[:300]}")
    if r.status_code == 400 or "maximum" in texto.lower() or "exceed" in texto.lower():
        if "bbox" in texto.lower() or "area" in texto.lower() or "exceed" in texto.lower():
            raise AreaError(texto[:300])
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"Respuesta no-JSON: {texto[:300]}")
    if isinstance(data, dict) and "error" in data:
        err = str(data["error"])
        if "area" in err.lower() or "exceed" in err.lower() or "large" in err.lower():
            raise AreaError(err[:300])
        raise RuntimeError(err[:300])
    return data


def nombre_de_calle(street):
    if not street:
        return ""
    if street.get("isEmpty"):
        return ""
    return (street.get("name") or "").strip()


def punto_medio(seg):
    geom = seg.get("geometry") or seg.get("geoJSONGeometry") or seg.get("geom") or {}
    coords = geom.get("coordinates") or []
    if not coords:
        return None, None
    p = coords[len(coords) // 2]
    try:
        return float(p[0]), float(p[1])
    except (TypeError, ValueError, IndexError):
        return None, None


def analizar_respuesta(data, tipos_con_nombre):
    """Extrae de la respuesta los segmentos sin nombre + sugerencia de nombre."""
    segs = _objetos(data, "segments")
    streets = {s.get("id"): s for s in _objetos(data, "streets")}
    cities = {c.get("id"): c for c in _objetos(data, "cities")}

    # nombre por segmento (para sugerencias) y conectividad por nodos
    nombre_seg = {}
    nodos = {}  # nodeID -> [segmento_ids]
    info = {}
    for seg in segs:
        sid = seg.get("id")
        if sid is None:
            continue
        st = streets.get(seg.get("primaryStreetID"))
        nombre_seg[sid] = nombre_de_calle(st)
        info[sid] = seg
        for nk in ("fromNodeID", "toNodeID"):
            nid = seg.get(nk)
            if nid is not None:
                nodos.setdefault(nid, []).append(sid)

    hallazgos = []
    for seg in segs:
        sid = seg.get("id")
        if sid is None:
            continue
        rt = seg.get("roadType")
        if rt not in tipos_con_nombre:
            continue
        if seg.get("junctionID"):  # rotonda: puede ir sin nombre
            continue
        if nombre_seg.get(sid):
            continue  # sí tiene nombre
        lon, lat = punto_medio(seg)
        if lon is None:
            continue

        # sugerencia estilo GAIA: nombre de la vialidad conectada que continúa
        sugerencias = {}
        for nk in ("fromNodeID", "toNodeID"):
            nid = seg.get(nk)
            for vecino in nodos.get(nid, []):
                if vecino == sid:
                    continue
                nom = nombre_seg.get(vecino, "")
                if not nom:
                    continue
                peso = 1
                if info[vecino].get("roadType") == rt:
                    peso += 2  # mismo tipo de vía: probable continuación
                sugerencias[nom] = sugerencias.get(nom, 0) + peso
        sugerido = max(sugerencias, key=sugerencias.get) if sugerencias else ""

        ciudad = ""
        st = streets.get(seg.get("primaryStreetID"))
        if st and st.get("cityID") in cities:
            c = cities[st.get("cityID")]
            if not c.get("isEmpty"):
                ciudad = (c.get("name") or "").strip()

        hallazgos.append({
            "id": sid,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "rt": rt,
            "ciudad": ciudad,
            "sug": sugerido,
        })
    return hallazgos, len(segs)


def escanear_bbox(sesion, env, bbox, tipos, pausa, contador, profundidad=0):
    """Escanea un bbox; si el servidor dice que es muy grande, lo parte en 4."""
    try:
        data = pedir_celda(sesion, env, bbox, pausa, tipos)
        contador["req"] += 1
        h, n = analizar_respuesta(data, tipos)
        contador["segs"] += n
        return h
    except AreaError:
        contador["req"] += 1
        if profundidad >= 5:
            return []
        x1, y1, x2, y2 = bbox
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        out = []
        for sub in ([x1, y1, mx, my], [mx, y1, x2, my],
                    [x1, my, mx, y2], [mx, my, x2, y2]):
            out.extend(escanear_bbox(sesion, env, sub, tipos, pausa,
                                     contador, profundidad + 1))
        return out


def detectar_entorno(cfg, pausa):
    """Averigua en qué servidor (usa/row) vive México y si hay acceso sin login."""
    errores = {}
    for env in ORDEN_ENTORNOS:
        try:
            s = nueva_sesion(env)
            data = pedir_celda(s, env, BBOX_PRUEBA, pausa)
            segs = _objetos(data, "segments")
            log(f"Entorno '{env}': {len(segs)} segmentos en la zona de prueba")
            if segs:
                return env, s
        except AreaError:
            # área excedida = el servidor SÍ respondió; probamos un bbox chico
            try:
                s = nueva_sesion(env)
                chico = [-103.37, 20.665, -103.36, 20.672]
                data = pedir_celda(s, env, chico, pausa)
                if _objetos(data, "segments"):
                    return env, s
            except Exception as e2:
                errores[env] = repr(e2)
        except Exception as e:
            errores[env] = repr(e)
            log(f"Entorno '{env}' falló: {e}")
    raise AuthError(f"Ningún servidor respondió con datos. Detalle: {errores}")


# ------------------------------------------------------------------- almacén
def cargar_almacen():
    """Carga docs/data/estados/*.json -> {estado: {seg_id_str: registro}}"""
    almacen = {}
    if os.path.isdir(ESTADOS_DIR):
        for fn in os.listdir(ESTADOS_DIR):
            if fn.endswith(".json"):
                d = load_json(os.path.join(ESTADOS_DIR, fn), {})
                estado = d.get("estado")
                if estado:
                    almacen[estado] = d.get("segmentos", {})
    return almacen


def guardar_almacen(almacen, meta):
    os.makedirs(ESTADOS_DIR, exist_ok=True)
    resumen_estados = []
    for estado, segmentos in sorted(almacen.items()):
        slug = slugify(estado)
        ciudades = {}
        for reg in segmentos.values():
            c = reg.get("ciudad") or "(sin ciudad)"
            ciudades[c] = ciudades.get(c, 0) + 1
        save_json(os.path.join(ESTADOS_DIR, f"{slug}.json"),
                  {"estado": estado, "segmentos": segmentos}, compact=True)
        resumen_estados.append({
            "estado": estado, "slug": slug, "total": len(segmentos),
            "ciudades": len(ciudades),
        })
    resumen = {
        "actualizado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "env": meta.get("env", "usa"),
        "total": sum(e["total"] for e in resumen_estados),
        "estados": resumen_estados,
        "progreso": meta.get("progreso", {}),
        "aviso": meta.get("aviso", ""),
        "tipos": {str(k): v for k, v in ROAD_TYPE_NAMES.items()},
    }
    save_json(os.path.join(DATA_DIR, "resumen.json"), resumen)
    return resumen


# ---------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modo", choices=["test", "completo"], default="completo")
    ap.add_argument("--minutos", type=float, default=240)
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH, {})
    bbox_mx = cfg.get("bbox", [-118.45, 14.5, -86.65, 32.75])
    celda = cfg.get("celda_grados", 0.2)
    pausa = cfg.get("pausa_segundos", 0.25)
    tipos = set(cfg.get("tipos_con_nombre", [1, 2, 3, 6, 7]))
    ciclos_vacia = cfg.get("reescanear_vacias_cada", 5)

    inicio = time.time()
    limite = inicio + args.minutos * 60
    estados_mx = EstadosMX(GEOJSON_PATH)
    estado = load_json(STATE_PATH, {})
    contador = {"req": 0, "segs": 0}

    # --- detección de entorno / acceso sin login
    try:
        if estado.get("env"):
            env = estado["env"]
            sesion = nueva_sesion(env)
        else:
            env, sesion = detectar_entorno(cfg, pausa)
            estado["env"] = env
        log(f"Usando entorno '{env}' (sin login, como el modo práctica)")
    except Exception as e:
        log(f"ERROR de acceso: {e}")
        os.makedirs(os.path.dirname(DEBUG_PATH), exist_ok=True)
        with open(DEBUG_PATH, "w", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()}\n{repr(e)}\n")
        almacen = cargar_almacen()
        guardar_almacen(almacen, {"env": estado.get("env", "usa"),
                                  "aviso": "No se pudo acceder a los datos de Waze en la última corrida. "
                                           "Revisa state/debug_ultimo_error.txt"})
        save_json(LASTRUN_PATH, {"ok": False, "error": repr(e),
                                 "fecha": datetime.now(timezone.utc).isoformat()})
        sys.exit(0)

    almacen = cargar_almacen()

    # --- malla
    lon1, lat1, lon2, lat2 = bbox_mx
    cols = max(1, math.ceil((lon2 - lon1) / celda))
    filas = max(1, math.ceil((lat2 - lat1) / celda))
    total_celdas = cols * filas
    celdas_info = estado.get("celdas", {})  # idx -> {"n": segs, "c": ciclo}
    cursor = estado.get("cursor", 0)
    ciclo = estado.get("ciclo", 1)

    if args.modo == "test":
        log("MODO PRUEBA: escaneando solo el centro de Guadalajara")
        celdas_a_escanear = [("test", BBOX_PRUEBA)]
    else:
        celdas_a_escanear = None  # se generan sobre la marcha

    def bbox_de(idx):
        f, c = divmod(idx, cols)
        x1 = lon1 + c * celda
        y1 = lat1 + f * celda
        m = 0.002  # margen para que las sugerencias vean calles vecinas
        return [x1 - m, y1 - m, min(x1 + celda, lon2) + m, min(y1 + celda, lat2) + m]

    escaneadas = []
    hallados_run = 0
    fallos_seguidos = 0
    try:
        if args.modo == "test":
            for nombre, bb in celdas_a_escanear:
                h = escanear_bbox(sesion, env, bb, tipos, pausa, contador)
                escaneadas.append(("test", bb, h))
                hallados_run += len(h)
        else:
            while time.time() < limite:
                if cursor >= total_celdas:
                    cursor = 0
                    ciclo += 1
                    log(f"Vuelta completa al país. Iniciando ciclo {ciclo}")
                idx = cursor
                cursor += 1
                info = celdas_info.get(str(idx))
                # celdas que salieron vacías se revisan solo de vez en cuando
                if info and info.get("n", 0) == 0 and ciclo - info.get("c", 0) < ciclos_vacia:
                    continue
                bb = bbox_de(idx)
                segs_antes = contador["segs"]
                try:
                    h = escanear_bbox(sesion, env, bb, tipos, pausa, contador)
                    fallos_seguidos = 0
                except AuthError:
                    raise
                except Exception as e:
                    fallos_seguidos += 1
                    log(f"Celda {idx} falló ({e}); se reintentará en la próxima corrida")
                    if fallos_seguidos >= 8:
                        log("Demasiados fallos seguidos; se detiene y se guarda el avance")
                        break
                    cursor = max(cursor, idx + 1)
                    time.sleep(3)
                    continue
                segs_en_celda = contador["segs"] - segs_antes
                celdas_info[str(idx)] = {"n": 1 if (h or segs_en_celda) else 0, "c": ciclo}
                escaneadas.append((str(idx), bb, h))
                hallados_run += len(h)
                if len(escaneadas) % 200 == 0:
                    log(f"{len(escaneadas)} celdas | {contador['req']} peticiones | "
                        f"{hallados_run} sin nombre | cursor {cursor}/{total_celdas}")
    except AuthError as e:
        log(f"El servidor dejó de aceptar peticiones: {e}")
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log(f"Error inesperado (se guarda el avance): {e}")

    # --- actualizar almacén: quitar lo viejo de las celdas re-escaneadas
    celdas_re = {c for c, _b, _h in escaneadas}
    if celdas_re:
        for est in list(almacen.keys()):
            seg_map = almacen[est]
            for k in [k for k, v in seg_map.items() if v.get("celda") in celdas_re]:
                del seg_map[k]
            if not seg_map:
                del almacen[est]

    for celda_id, _bb, hallazgos in escaneadas:
        for h in hallazgos:
            est = estados_mx.estado_de(h["lon"], h["lat"])
            reg = dict(h)
            reg["celda"] = celda_id
            almacen.setdefault(est, {})[str(h["id"])] = reg

    celdas_hechas = len([1 for v in celdas_info.values()])
    progreso = {
        "ciclo": ciclo,
        "celdas_escaneadas": celdas_hechas,
        "celdas_total": total_celdas,
        "porcentaje": round(100.0 * min(celdas_hechas, total_celdas) / total_celdas, 1),
        "modo": args.modo,
    }
    resumen = guardar_almacen(almacen, {"env": env, "progreso": progreso})

    estado.update({"cursor": cursor, "ciclo": ciclo, "celdas": celdas_info, "env": env})
    save_json(STATE_PATH, estado, compact=True)
    save_json(LASTRUN_PATH, {
        "ok": True, "fecha": datetime.now(timezone.utc).isoformat(),
        "modo": args.modo, "celdas": len(escaneadas),
        "peticiones": contador["req"], "segmentos_vistos": contador["segs"],
        "sin_nombre_en_corrida": hallados_run, "total_acumulado": resumen["total"],
        "minutos": round((time.time() - inicio) / 60, 1),
    })
    log(f"Listo: {len(escaneadas)} celdas, {contador['req']} peticiones, "
        f"{hallados_run} segmentos sin nombre en esta corrida, "
        f"{resumen['total']} acumulados en total.")


if __name__ == "__main__":
    main()
