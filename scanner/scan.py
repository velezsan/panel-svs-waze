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
import subprocess
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
# 1 Calle (ST), 2 Avenida principal (PS)
ROAD_TYPE_NAMES = {
    1: "Calle", 2: "Avenida principal", 3: "Autopista", 4: "Rampa",
    5: "Sendero peatonal", 6: "Carretera mayor", 7: "Carretera menor",
    8: "Terracería", 10: "Andador", 15: "Ferry", 16: "Escaleras",
    17: "Camino privado", 18: "Vía de tren", 19: "Pista de aterrizaje",
    20: "Camino de estacionamiento", 22: "Callejón",
}

BBOX_PRUEBA = [-103.43, 20.655, -103.28, 20.695]  # Guadalajara centro (triple)

# Línea fronteriza México-EUA (aprox). Cerca de ella hay una franja cuyos
# datos viven en el servidor NA; esas celdas se verifican contra ambos.
FRONTERA_USA = [
    (-117.13, 32.54), (-114.72, 32.72), (-111.07, 31.33), (-108.21, 31.33),
    (-108.21, 31.78), (-106.53, 31.79), (-104.92, 30.61), (-104.40, 29.57),
    (-103.11, 29.03), (-102.40, 29.85), (-101.40, 29.77), (-100.96, 29.35),
    (-99.51, 27.60), (-99.11, 26.42), (-98.20, 26.06), (-97.14, 25.87),
]


def distancia_a_frontera(lon, lat):
    """Distancia aproximada (en grados) del punto a la frontera con EUA."""
    best = 1e9
    for (x1, y1), (x2, y2) in zip(FRONTERA_USA, FRONTERA_USA[1:]):
        dx, dy = x2 - x1, y2 - y1
        den = dx * dx + dy * dy
        t = 0.0 if den == 0 else max(0.0, min(1.0, ((lon - x1) * dx + (lat - y1) * dy) / den))
        px, py = x1 + t * dx, y1 + t * dy
        d = ((lon - px) ** 2 + (lat - py) ** 2) ** 0.5
        if d < best:
            best = d
    return best

# ------------------------------------------------------------- INEGI (GAIA)
# Réplica de la consulta del script "WME INEGI GAIA": WMS GetFeatureInfo
# sobre la capa de vialidades c112, en hasta 5 puntos del segmento.
INEGI_WMS = "https://gaia.inegi.org.mx/NLB/tunnel/wms/wms61"
INEGI_LAYER = "c112"
INEGI_DELTA = 0.00004

BLACKLIST_INEGI = {
    "calle", "calles", "avenida", "av", "privada", "priv", "cda", "cerrada",
    "retorno", "andador", "vialidad", "vialidades", "no", "si", "null", "s/n",
    "sin nombre", "callejón", "bulevar", "boulevard", "blvd", "camino",
    "carretera", "autopista", "periférico", "circuito", "viaducto", "calzada",
    "prolongación", "diagonal", "paseo", "0", "1",
    "mapserver", "inegi", "nombre", "nomvial", "tipovial", "texto",
    "objectid", "shape", "length", "cvegeo", "shape_length",
    "the_geom", "cve_mun", "cve_loc", "cve_ent", "ambito",
}

RE_TH_TD = re.compile(r"<th[^>]*>([\s\S]*?)</th>\s*<td[^>]*>([\s\S]*?)</td>", re.I)
RE_TD = re.compile(r"<td[^>]*>([\s\S]*?)</td>", re.I)
RE_TAGS = re.compile(r"<[^>]+>")

_MINUSCULAS = {"de", "del", "la", "las", "los", "el", "en", "y", "a", "e", "o", "u"}


def _title_case(texto):
    """Réplica de toTitleCase de GAIA (minúsculas conectoras, romanos en alta)."""
    palabras = texto.lower().split()
    out = []
    for i, w in enumerate(palabras):
        if i > 0 and w in _MINUSCULAS:
            out.append(w)
        elif re.fullmatch(r"[ivxlcdm]{2,7}", w):
            out.append(w.upper())
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)


def _normalizar_nombre(nombre):
    """Réplica de normalizeStreetName de GAIA ("31"→"Calle 31", Avenida→Av., etc.)."""
    if not nombre:
        return nombre
    t = nombre.strip()
    t = re.sub(r"^C\.?\s+", "", t, flags=re.I)
    t = re.sub(r"^Avenida\b", "Av.", t, flags=re.I)
    t = re.sub(r"^Boulevard\b", "Blvd.", t, flags=re.I)
    t = re.sub(r"\bGeneral\b", "Gral.", t, flags=re.I)
    t = re.sub(r"\bPriv\.?\b", "Privada", t, flags=re.I)
    t = re.sub(r"^(\d+)(?:ta|da|ra|va|na|ma)\.?\b", r"\1a.", t, flags=re.I)
    t = re.sub(r"^(\d+)(?:to|do|ro|vo|no|mo|er)\.?\b", r"\1o.", t, flags=re.I)
    if not re.match(r"^\d", t):
        return t
    if re.match(r"^\d+[ao]\.\s", t) or re.fullmatch(r"\d+[ao]\.", t):
        return t
    m = re.match(r"^(\d+-[A-Za-z0-9]*)\s*(.*)$", t) or re.match(r"^(\d+)\s*(.*)$", t)
    if not m:
        return t
    num, resto = m.group(1), (m.group(2) or "").strip()
    if re.match(r"^de\b", resto, flags=re.I):
        return t
    if not resto:
        return "Calle " + num
    return "Calle " + num + " " + resto


def _limpiar_valor(val, nombres, vistos):
    """Réplica de addClean de GAIA: filtra IDs, coordenadas y palabras basura."""
    if not val or len(val) > 100:
        return
    v = val.strip()
    if re.match(r"^\d+\.\d{4,}", v):
        return  # coordenadas
    if re.fullmatch(r"\d{4,}", v):
        return  # IDs numéricos largos
    if re.fullmatch(r"\d+\s+[A-Za-z]", v) and int(re.match(r"^\d+", v).group()) > 999:
        return
    if v.lower() in BLACKLIST_INEGI:
        return
    if re.search(r"mapserver|inegi|objectid|shape_len|cvegeo", v, flags=re.I):
        return
    limpio = _normalizar_nombre(_title_case(v))
    if not limpio or len(limpio) < 2:
        return
    if re.fullmatch(r"[A-Za-z]{1,3}\d+", limpio):
        return  # códigos tipo "A1"
    m = re.match(r"^Calle (\d+)", limpio)
    if m and int(m.group(1)) > 999:
        return
    if limpio.lower() not in vistos:
        vistos.add(limpio.lower())
        nombres.append(limpio)


def parse_wms(html):
    """Réplica de parseWMS de GAIA: extrae nombres de vialidad del HTML."""
    if not html or len(html) < 30:
        return []
    nombres, vistos = [], set()
    for m in RE_TH_TD.finditer(html):
        header = RE_TAGS.sub("", m.group(1)).strip().lower()
        val = RE_TAGS.sub("", m.group(2)).strip()
        if (header in ("nomvial", "nombre", "nom_calle", "nombre_vialidad", "nom_vial")
                or re.search(r"vial", header, flags=re.I)
                or re.match(r"^nom_?v", header, flags=re.I)
                or re.match(r"^nombre", header, flags=re.I)):
            _limpiar_valor(val, nombres, vistos)
    if not nombres:
        for m in RE_TD.finditer(html):
            _limpiar_valor(RE_TAGS.sub("", m.group(1)).strip(), nombres, vistos)
    # si un nombre es prefijo de otro más largo, gana el más específico
    filtrados = []
    for f, nf in enumerate(nombres):
        es_prefijo = any(
            g != f and ng.lower().startswith(nf.lower()) and len(ng) > len(nf)
            for g, ng in enumerate(nombres)
        )
        if not es_prefijo:
            filtrados.append(nf)
    return filtrados


def _nombre_invalido(n):
    return bool(re.fullmatch(r"ninguno|sin nombre|n/a|s/n", n, flags=re.I))


def puntos_consulta(coords):
    """Réplica de getQueryPoints: inicio, ¼, mitad, ¾ y fin de la geometría."""
    n = len(coords)
    if n == 0:
        return []
    if n == 1:
        return [coords[0]]
    indices = [0]
    if n > 2:
        indices.append(int(n * 0.25))
    indices.append(int(n * 0.5))
    if n > 2:
        indices.append(int(n * 0.75))
    indices.append(n - 1)
    unicos, vistos = [], set()
    for i in indices:
        i = min(i, n - 1)
        if i not in vistos:
            vistos.add(i)
            unicos.append(coords[i])
    return unicos


class ConsultorINEGI:
    """Consulta el WMS del INEGI con caché por coordenada y pausa entre llamadas."""

    def __init__(self, pausa=0.15):
        self.sesion = requests.Session()
        self.sesion.headers.update({"User-Agent": HEADERS["User-Agent"]})
        self.cache = {}
        self.pausa = pausa
        self.peticiones = 0
        self.errores = 0

    def nombres_en(self, lon, lat):
        key = (round(lon, 6), round(lat, 6))
        if key in self.cache:
            return self.cache[key]
        bbox = (f"{lon - INEGI_DELTA:.6f},{lat - INEGI_DELTA:.6f},"
                f"{lon + INEGI_DELTA:.6f},{lat + INEGI_DELTA:.6f}")
        params = {
            "SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetFeatureInfo",
            "LAYERS": INEGI_LAYER, "QUERY_LAYERS": INEGI_LAYER,
            "SRS": "EPSG:4326", "BBOX": bbox,
            "WIDTH": "5", "HEIGHT": "5", "X": "2", "Y": "2",
            "INFO_FORMAT": "text/html", "FEATURE_COUNT": "5",
        }
        time.sleep(self.pausa)
        self.peticiones += 1
        try:
            r = self.sesion.get(INEGI_WMS, params=params, timeout=10)
            r.encoding = "utf-8"  # el INEGI responde UTF-8 sin declararlo
            nombres = parse_wms(r.text) if r.status_code == 200 else []
            if r.status_code == 200:
                self.cache[key] = nombres
            else:
                self.errores += 1
            return nombres
        except requests.RequestException:
            self.errores += 1
            return []

    def sugerir(self, coords):
        """Réplica del flujo por etapas de GAIA con aborto temprano.

        Devuelve (nombre, confianza 0-100, empatado). La confianza es el % de
        puntos del segmento donde el INEGI regresó ese mismo nombre.
        """
        pts = puntos_consulta(coords)
        if not pts:
            return "", 0, False
        total = len(pts)
        conteo = {}
        candidatos = None
        respondidos = 0
        for i, p in enumerate(pts):
            nombres = self.nombres_en(p[0], p[1])
            if i == 0 and not nombres:
                # la sonda regresó vacío: puede ser zona sin cobertura o un
                # rechazo silencioso del INEGI; reintentamos una vez
                self.cache.pop((round(p[0], 6), round(p[1], 6)), None)
                time.sleep(max(self.pausa, 0.4))
                nombres = self.nombres_en(p[0], p[1])
            respondidos += 1
            for n in nombres:
                conteo[n] = conteo.get(n, 0) + 1
            validos = [n for n in nombres if not _nombre_invalido(n)]
            if candidatos is None:
                candidatos = validos
            else:
                lset = {n.lower() for n in validos}
                candidatos = [n for n in candidatos if n.lower() in lset]
            if not candidatos:
                break  # el 100% ya es imposible (o zona sin cobertura): abortar
        mejor, mejor_n = None, 0
        for n, c in conteo.items():
            if not _nombre_invalido(n) and c > mejor_n:
                mejor, mejor_n = n, c
        conf = round(100.0 * mejor_n / total) if mejor else 0
        perfectos = sum(1 for n, c in conteo.items()
                        if not _nombre_invalido(n) and round(100.0 * c / total) >= 100)
        return (mejor or "", conf, perfectos > 1)


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

    def dentro_de_alguno(self, lon, lat):
        """True solo si el punto cae DENTRO de algún estado (sin 'más cercano')."""
        key = ("in", round(lon, 3), round(lat, 3))
        if key in self._cache:
            return self._cache[key]
        res = False
        for _name, rings in self.features:
            for (minx, miny, maxx, maxy, ring) in rings:
                if minx <= lon <= maxx and miny <= lat <= maxy and self._inside(lon, lat, ring):
                    res = True
                    break
            if res:
                break
        self._cache[key] = res
        return res


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
    # pedimos también los tipos vecinos (avenidas, carreteras, rampas) para
    # que las sugerencias de nombre tengan contexto, aunque no se reporten
    solicitados = sorted(set(tipos or [1, 2]) | {1, 2, 3, 4, 6, 7})
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


def largo_metros(seg):
    """Longitud del segmento en metros (usa el dato del servidor o la geometría)."""
    largo = seg.get("length")
    if isinstance(largo, (int, float)) and largo > 0:
        return float(largo)
    geom = seg.get("geometry") or seg.get("geoJSONGeometry") or {}
    coords = geom.get("coordinates") or []
    total = 0.0
    for a, b in zip(coords, coords[1:]):
        try:
            lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
        except (TypeError, ValueError, IndexError):
            return None
        h = (math.sin((lat2 - lat1) / 2) ** 2
             + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
        total += 2 * 6371000 * math.asin(math.sqrt(h))
    return total if total > 0 else None


def analizar_respuesta(data, tipos_con_nombre, min_metros=0):
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
        if min_metros:
            largo = largo_metros(seg)
            if largo is not None and largo < min_metros:
                continue  # segmento muy corto: no se reporta
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

        geom = seg.get("geometry") or seg.get("geoJSONGeometry") or {}
        hallazgos.append({
            "id": sid,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "rt": rt,
            "ciudad": ciudad,
            "sug": "",          # nombre según INEGI (se llena después)
            "conf": 0,           # % de confianza INEGI
            "vec": sugerido,     # respaldo: nombre de la vialidad vecina
            "_coords": geom.get("coordinates") or [],
        })
    return hallazgos, len(segs)


def enriquecer_con_inegi(hallazgos, inegi, limite, previas=None):
    """Consulta el INEGI (como GAIA) para cada segmento sin nombre encontrado.

    Solo se conservan los segmentos con nombre del INEGI al 100% y sin
    empate (los mismos que GAIA marca como aplicables). Si el INEGI no
    responde hoy pero el segmento ya tenía nombre al 100% en una corrida
    anterior, se conserva ese nombre (el INEGI a veces contesta vacío).
    """
    for h in hallazgos:
        coords = h.pop("_coords", [])
        if inegi is None or time.time() >= limite:
            continue
        nombre, conf, empatado = inegi.sugerir(coords)
        # como GAIA: solo valen los resultados al 100% y sin empate
        if nombre and conf >= 100 and not empatado:
            h["sug"] = nombre
            h["conf"] = 100
        elif previas and str(h["id"]) in previas:
            h["sug"] = previas[str(h["id"])]
            h["conf"] = 100
    if inegi is None:
        return hallazgos
    return [h for h in hallazgos if h.get("sug")]


def escanear_bbox(sesion, env, bbox, tipos, pausa, contador, profundidad=0, min_metros=0):
    """Escanea un bbox; si el servidor dice que es muy grande, lo parte en 4."""
    try:
        data = pedir_celda(sesion, env, bbox, pausa, tipos)
        contador["req"] += 1
        h, n = analizar_respuesta(data, tipos, min_metros)
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
                                     contador, profundidad + 1, min_metros))
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
    slugs_actuales = set()
    for estado, segmentos in sorted(almacen.items()):
        slug = slugify(estado)
        slugs_actuales.add(slug)
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
    # borrar archivos de estados que ya no tienen segmentos
    for fn in os.listdir(ESTADOS_DIR):
        if fn.endswith(".json") and fn[:-5] not in slugs_actuales:
            os.remove(os.path.join(ESTADOS_DIR, fn))
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
    ap.add_argument("--zona", default="",
                    help='Re-escaneo prioritario: nombre de estado, "lat,lon" o "lon1,lat1,lon2,lat2"')
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH, {})
    bbox_mx = cfg.get("bbox", [-118.45, 14.5, -86.65, 32.75])
    celda = cfg.get("celda_grados", 0.2)
    pausa = cfg.get("pausa_segundos", 0.25)
    tipos = set(cfg.get("tipos_con_nombre", [1]))
    min_metros = cfg.get("longitud_minima_metros", 30)
    ciclos_vacia = cfg.get("reescanear_vacias_cada", 5)
    solo_estados = set(cfg.get("solo_estados") or [])
    franja_umbral = cfg.get("franja_umbral_grados", 0.5)
    usar_inegi = cfg.get("sugerencias_inegi", True)
    inegi = ConsultorINEGI(cfg.get("pausa_inegi_segundos", 0.15)) if usar_inegi else None

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
    # nombres INEGI al 100% de corridas anteriores (memoria anti-fallas)
    sugs_previas = {}
    for _est, _m in almacen.items():
        for _k, _v in _m.items():
            if _v.get("sug"):
                sugs_previas[_k] = _v["sug"]

    # --- malla
    lon1, lat1, lon2, lat2 = bbox_mx
    cols = max(1, math.ceil((lon2 - lon1) / celda))
    filas = max(1, math.ceil((lat2 - lat1) / celda))
    total_celdas = cols * filas
    # normalizar los ids de celda del almacén al grid actual (por si cambió la malla,
    # p. ej. al pasar de un estado al país completo, sin perder lo ya encontrado)
    for _est, _m in almacen.items():
        for _v in _m.values():
            _c = int((_v["lon"] - lon1) // celda)
            _f = int((_v["lat"] - lat1) // celda)
            if 0 <= _c < cols and 0 <= _f < filas:
                _v["celda"] = str(_f * cols + _c)
    celdas_info = estado.get("celdas", {})  # idx -> {"n": segs, "c": ciclo}
    cursor = estado.get("cursor", 0)
    ciclo = estado.get("ciclo", 1)
    if (estado.get("celda_grados") not in (None, celda)
            or estado.get("bbox_escaneo") not in (None, bbox_mx)):
        log("Cambió la zona o el tamaño de celda: reiniciando el recorrido")
        celdas_info, cursor, ciclo = {}, 0, 1

    if args.modo == "test":
        log("MODO PRUEBA: escaneando solo el centro de Guadalajara")
        # en rebanadas de 0.05° porque el servidor recorta las áreas grandes
        celdas_a_escanear = []
        _x1, _y1, _x2, _y2 = BBOX_PRUEBA
        _x, _i = _x1, 0
        while _x < _x2:
            celdas_a_escanear.append((f"test{_i}", [_x, _y1, min(_x + 0.05, _x2), _y2]))
            _x += 0.05
            _i += 1
    else:
        celdas_a_escanear = None  # se generan sobre la marcha

    def bbox_de(idx):
        f, c = divmod(idx, cols)
        x1 = lon1 + c * celda
        y1 = lat1 + f * celda
        m = 0.002  # margen para que las sugerencias vean calles vecinas
        return [x1 - m, y1 - m, min(x1 + celda, lon2) + m, min(y1 + celda, lat2) + m]

    # --- re-escaneo prioritario de una zona (estado, punto o bbox)
    indices_zona = []
    if args.zona.strip() and args.modo == "completo":
        z = args.zona.strip()
        estados_nombres = {n.lower(): n for n, _r in estados_mx.features}
        zona_bbox = None
        zona_estado = None
        partes = [p.strip() for p in z.split(",")]
        if z.lower() in estados_nombres:
            zona_estado = estados_nombres[z.lower()]
        elif len(partes) == 4:
            try:
                a, b, c, d = map(float, partes)
                zona_bbox = [min(a, c), min(b, d), max(a, c), max(b, d)]
            except ValueError:
                pass
        elif len(partes) == 2:
            try:
                la, lo = map(float, partes)  # formato lat,lon (como el WME)
                zona_bbox = [lo - 0.15, la - 0.15, lo + 0.15, la + 0.15]
            except ValueError:
                pass
        for idx in range(total_celdas):
            f, c = divmod(idx, cols)
            x1 = lon1 + c * celda
            y1 = lat1 + f * celda
            x2, y2 = min(x1 + celda, lon2), min(y1 + celda, lat2)
            if zona_bbox is not None:
                if x1 <= zona_bbox[2] and x2 >= zona_bbox[0] and y1 <= zona_bbox[3] and y2 >= zona_bbox[1]:
                    indices_zona.append(idx)
            elif zona_estado is not None:
                mxp, myp = (x1 + x2) / 2, (y1 + y2) / 2
                pts9 = [(mxp, myp), (x1, y1), (x2, y1), (x1, y2), (x2, y2),
                        (mxp, y1), (mxp, y2), (x1, myp), (x2, myp)]
                if any(estados_mx.estado_de(px, py) == zona_estado for px, py in pts9):
                    indices_zona.append(idx)
        if indices_zona:
            log(f"RE-ESCANEO PRIORITARIO de '{z}': {len(indices_zona)} celdas en cola")
        else:
            log(f"Zona '{z}' no reconocida (usa nombre de estado, lat,lon o bbox); "
                "se hará el barrido normal")

    escaneadas = []
    hallados_run = 0
    fallos_seguidos = 0
    sesion_usa = None
    celdas_run = set()
    pub_cada = cfg.get("publicar_cada_minutos", 40)
    ultima_pub = inicio
    celdas_aplicadas = 0

    def aplicar_pendientes():
        """Vuelca lo escaneado al almacén y guarda los archivos del panel."""
        nonlocal escaneadas, celdas_aplicadas
        celdas_aplicadas += len(escaneadas)
        hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # fecha en que se detectó cada segmento por primera vez (se conserva)
        vistos_prev = {}
        for _e, _m in almacen.items():
            for _k, _v in _m.items():
                vistos_prev[_k] = _v.get("visto", "")
        celdas_re = {c for c, _b, _h in escaneadas}
        es_test = args.modo == "test"
        if celdas_re:
            for est in list(almacen.keys()):
                seg_map = almacen[est]
                for k in [k for k, v in seg_map.items()
                          if v.get("celda") in celdas_re
                          or (es_test and str(v.get("celda", "")).startswith("test"))]:
                    del seg_map[k]
                if not seg_map:
                    del almacen[est]
        for celda_id, _bb, hallazgos in escaneadas:
            for h in hallazgos:
                est = estados_mx.estado_de(h["lon"], h["lat"])
                if solo_estados and est not in solo_estados:
                    continue
                reg = dict(h)
                reg["celda"] = celda_id
                sid = str(h["id"])
                reg["visto"] = vistos_prev[sid] if sid in vistos_prev else hoy
                almacen.setdefault(est, {})[sid] = reg
        escaneadas = []
        celdas_hechas = len([1 for v in celdas_info.values()])
        progreso = {
            "ciclo": ciclo,
            "celdas_escaneadas": celdas_hechas,
            "celdas_total": total_celdas,
            "porcentaje": round(100.0 * min(celdas_hechas, total_celdas) / total_celdas, 1),
            "modo": args.modo,
        }
        resumen = guardar_almacen(almacen, {"env": env, "progreso": progreso})
        estado.update({"cursor": cursor, "ciclo": ciclo, "celdas": celdas_info,
                       "env": env, "celda_grados": celda, "bbox_escaneo": bbox_mx})
        save_json(STATE_PATH, estado, compact=True)
        return resumen

    def publicar_git(etiqueta):
        """Commit y push intermedios (solo dentro de GitHub Actions)."""
        if not os.environ.get("GITHUB_ACTIONS"):
            return
        try:
            subprocess.run(["git", "config", "user.name", "escaner-bot"], cwd=BASE, check=False)
            subprocess.run(["git", "config", "user.email",
                            "actions@users.noreply.github.com"], cwd=BASE, check=False)
            subprocess.run(["git", "add", "docs/data", "state"], cwd=BASE, check=False)
            r = subprocess.run(["git", "commit", "-m", etiqueta], cwd=BASE,
                               capture_output=True)
            if r.returncode == 0:
                subprocess.run(["git", "pull", "--rebase", "origin", "main"],
                               cwd=BASE, check=False)
                subprocess.run(["git", "push"], cwd=BASE, check=False)
                log(f"Publicación parcial hecha: {etiqueta}")
        except Exception as e:
            log(f"No se pudo publicar parcial ({e}); se publicará al final")

    try:
        if args.modo == "test":
            for nombre, bb in celdas_a_escanear:
                h = escanear_bbox(sesion, env, bb, tipos, pausa, contador,
                                  min_metros=min_metros)
                h = enriquecer_con_inegi(h, inegi, limite, sugs_previas)
                escaneadas.append((nombre, bb, h))
                hallados_run += len(h)
        elif indices_zona:
            for idx in indices_zona:
                if time.time() >= limite:
                    log("Se acabó el tiempo; el resto de la zona queda para otra corrida")
                    break
                bb = bbox_de(idx)
                x1c, y1c, x2c, y2c = bb
                mxc, myc = (x1c + x2c) / 2, (y1c + y2c) / 2
                usa_n = None
                if distancia_a_frontera(mxc, myc) < franja_umbral:
                    try:
                        if sesion_usa is None:
                            sesion_usa = nueva_sesion("usa")
                        d_usa = pedir_celda(sesion_usa, "usa", bb, pausa, tipos)
                        contador["req"] += 1
                        usa_n = len(_objetos(d_usa, "segments"))
                    except Exception:
                        usa_n = None
                segs_antes = contador["segs"]
                try:
                    h = escanear_bbox(sesion, env, bb, tipos, pausa, contador,
                                      min_metros=min_metros)
                    fallos_seguidos = 0
                except AuthError:
                    raise
                except Exception as e:
                    fallos_seguidos += 1
                    log(f"Celda {idx} falló ({e})")
                    if fallos_seguidos >= 8:
                        log("Demasiados fallos seguidos; se detiene y se guarda el avance")
                        break
                    time.sleep(3)
                    continue
                segs_en_celda = contador["segs"] - segs_antes
                if usa_n is not None and usa_n > segs_en_celda:
                    h = []
                    segs_en_celda = 0
                else:
                    h = enriquecer_con_inegi(h, inegi, limite, sugs_previas)
                celdas_info[str(idx)] = {"n": 1 if (h or segs_en_celda) else 0, "c": ciclo}
                escaneadas.append((str(idx), bb, h))
                hallados_run += len(h)
                if len(escaneadas) % 200 == 0:
                    log(f"zona: {len(escaneadas)}/{len(indices_zona)} celdas | "
                        f"{hallados_run} sin nombre")
                if pub_cada and time.time() - ultima_pub >= pub_cada * 60 and escaneadas:
                    aplicar_pendientes()
                    publicar_git(f"Publicación parcial {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
                    ultima_pub = time.time()
        else:
            while time.time() < limite:
                if cursor >= total_celdas:
                    cursor = 0
                    ciclo += 1
                    log(f"Vuelta completa al país. Iniciando ciclo {ciclo}")
                idx = cursor
                cursor += 1
                if str(idx) in celdas_run:
                    log("Toda la zona quedó cubierta en esta corrida; terminando")
                    break
                celdas_run.add(str(idx))
                info = celdas_info.get(str(idx))
                # celdas que salieron vacías se revisan solo de vez en cuando
                if info and info.get("n", 0) == 0 and ciclo - info.get("c", 0) < ciclos_vacia:
                    continue
                bb = bbox_de(idx)
                # saltar celdas fuera de México (o fuera de los estados elegidos)
                x1c, y1c, x2c, y2c = bb
                mxc, myc = (x1c + x2c) / 2, (y1c + y2c) / 2
                puntos_chk = [(mxc, myc), (x1c, y1c), (x2c, y1c), (x1c, y2c), (x2c, y2c),
                              (mxc, y1c), (mxc, y2c), (x1c, myc), (x2c, myc)]
                if solo_estados:
                    dentro = any(estados_mx.estado_de(px, py) in solo_estados
                                 for px, py in puntos_chk)
                else:
                    dentro = any(estados_mx.dentro_de_alguno(px, py)
                                 for px, py in puntos_chk)
                if not dentro:
                    celdas_info[str(idx)] = {"n": 0, "c": ciclo}
                    continue
                # franja fronteriza: si el servidor NA tiene más datos ahí,
                # esa zona vive en el otro servidor y no se debe reportar
                usa_n = None
                if distancia_a_frontera(mxc, myc) < franja_umbral:
                    try:
                        if sesion_usa is None:
                            sesion_usa = nueva_sesion("usa")
                        d_usa = pedir_celda(sesion_usa, "usa", bb, pausa, tipos)
                        contador["req"] += 1
                        usa_n = len(_objetos(d_usa, "segments"))
                    except Exception:
                        usa_n = None
                segs_antes = contador["segs"]
                try:
                    h = escanear_bbox(sesion, env, bb, tipos, pausa, contador,
                                      min_metros=min_metros)
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
                if usa_n is not None and usa_n > segs_en_celda:
                    log(f"Celda {idx}: franja del servidor NA "
                        f"({usa_n} segs en NA vs {segs_en_celda} en ROW); se omite")
                    h = []
                    segs_en_celda = 0  # tratarla como vacía: re-checar solo de vez en cuando
                else:
                    h = enriquecer_con_inegi(h, inegi, limite, sugs_previas)
                celdas_info[str(idx)] = {"n": 1 if (h or segs_en_celda) else 0, "c": ciclo}
                escaneadas.append((str(idx), bb, h))
                hallados_run += len(h)
                if len(escaneadas) % 200 == 0:
                    log(f"{len(escaneadas)} celdas | {contador['req']} peticiones | "
                        f"{hallados_run} sin nombre | cursor {cursor}/{total_celdas}")
                if pub_cada and time.time() - ultima_pub >= pub_cada * 60 and escaneadas:
                    aplicar_pendientes()
                    publicar_git(f"Publicación parcial {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
                    ultima_pub = time.time()
    except AuthError as e:
        log(f"El servidor dejó de aceptar peticiones: {e}")
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log(f"Error inesperado (se guarda el avance): {e}")

    # --- volcado final al almacén y archivos del panel
    resumen = aplicar_pendientes()
    save_json(LASTRUN_PATH, {
        "ok": True, "fecha": datetime.now(timezone.utc).isoformat(),
        "modo": args.modo, "celdas": celdas_aplicadas,
        "peticiones": contador["req"], "segmentos_vistos": contador["segs"],
        "sin_nombre_en_corrida": hallados_run, "total_acumulado": resumen["total"],
        "peticiones_inegi": inegi.peticiones if inegi else 0,
        "errores_inegi": inegi.errores if inegi else 0,
        "minutos": round((time.time() - inicio) / 60, 1),
    })
    log(f"Listo: {celdas_aplicadas} celdas, {contador['req']} peticiones, "
        f"{hallados_run} segmentos sin nombre en esta corrida, "
        f"{resumen['total']} acumulados en total.")


if __name__ == "__main__":
    main()
