# Panel SVS Waze · Segmentos sin nombre (México)

Página web que muestra los segmentos **sin nombre** del mapa de Waze en México,
al estilo de los paneles de la comunidad (como wmebr.info), con:

- Tabla por **estado** y **ciudad**, con filtros y buscador.
- **Permalink** que abre cada segmento directo en el Waze Map Editor.
- **Nombre sugerido** para cada segmento (el nombre de la vialidad con la que
  conecta, igual que la pista que da GAIA).
- Actualización **automática diaria** con GitHub Actions.

## ¿Cómo funciona?

1. `scanner/scan.py` recorre México en una malla de ~14,600 celdas y consulta la
   API pública del Waze Map Editor (la misma que usa el **modo práctica**, así
   que no necesita iniciar sesión).
2. Filtra los segmentos que deberían tener nombre y no lo tienen
   (calles, avenidas, autopistas y carreteras; ignora rotondas, rampas,
   estacionamientos, etc.).
3. Guarda los resultados en `docs/data/` (un archivo por estado).
4. GitHub Pages sirve `docs/index.html`, que lee esos datos y los muestra.
5. El workflow `.github/workflows/escaneo.yml` repite todo esto a diario a las
   03:17 (hora de Ciudad de México). Cada corrida avanza por donde iba la
   anterior, así que la cobertura del país crece día con día.

## Archivos

| Archivo | Qué es |
|---|---|
| `scanner/scan.py` | El escáner |
| `scanner/config.json` | Configuración (zona, tipos de vía, pausas) |
| `scanner/mx_estados.geojson` | Polígonos de los 32 estados (para clasificar) |
| `docs/index.html` | La página web |
| `docs/data/` | Resultados (los genera el escáner) |
| `state/` | Avance del escaneo entre corridas |
| `.github/workflows/escaneo.yml` | La corrida automática diaria |

## Configuración rápida

En `scanner/config.json`:

- `tipos_con_nombre`: tipos de vía que se revisan (1 Calle, 2 Avenida principal,
  3 Autopista, 6 Carretera mayor, 7 Carretera menor; agrega `8` si también
  quieres terracerías).
- `bbox`: recorte del país; puedes reducirlo a tu zona para escanear más rápido.
- `pausa_segundos`: pausa entre peticiones (sé amable con los servidores 🙂).

Lee `INSTRUCCIONES.md` para ponerlo en marcha paso a paso.
