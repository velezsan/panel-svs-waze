# Puesta en marcha, paso a paso 🚀

No necesitas saber programar. Son 5 pasos y solo se hacen una vez.
Al final tendrás una página pública que se actualiza sola todos los días.

---

## Paso 1 · Crear tu cuenta de GitHub (si no tienes)

1. Entra a **https://github.com/signup**
2. Regístrate con tu correo. Elige un nombre de usuario (por ejemplo `santiagovlz`);
   la dirección de tu página será `TU-USUARIO.github.io/panel-svs-waze`.

## Paso 2 · Crear el repositorio

1. Ya con sesión iniciada, entra a **https://github.com/new**
2. En **Repository name** escribe: `panel-svs-waze`
3. Deja la opción **Public** marcada (necesario para que GitHub Pages y las
   corridas automáticas sean gratis).
4. **No** marques "Add a README file".
5. Botón verde **Create repository**.

## Paso 3 · Subir los archivos

1. Descomprime el archivo `panel-svs-waze.zip` en tu computadora.
2. En la página de tu repositorio recién creado, haz clic en el enlace
   **"uploading an existing file"**.
3. Abre la carpeta descomprimida en Finder. Para que se vean las carpetas
   ocultas (la carpeta `.github` está oculta), presiona **Cmd + Shift + .**
   (comando + shift + punto).
4. Selecciona **todo** el contenido de la carpeta (incluida `.github`) y
   arrástralo a la página de GitHub.
5. Espera a que suban todos los archivos y haz clic en **Commit changes**.

> ¿No subió la carpeta `.github`? Plan B: en tu repositorio ve a
> **Add file → Create new file**, en el nombre escribe exactamente
> `.github/workflows/escaneo.yml` y pega el contenido del archivo
> `escaneo.yml` que viene en el zip (ábrelo con TextEdit para copiarlo).

## Paso 4 · Encender la página (GitHub Pages)

1. En tu repositorio: **Settings** (⚙️) → menú izquierdo **Pages**.
2. En **Source** deja "Deploy from a branch".
3. En **Branch** elige `main`, y en la carpeta elige **`/docs`**. Clic en **Save**.
4. En 1–2 minutos tu página estará viva en:
   **`https://TU-USUARIO.github.io/panel-svs-waze/`**
   (la URL exacta aparece ahí mismo, en Settings → Pages).

## Paso 5 · Correr el primer escaneo (prueba)

1. En tu repositorio ve a la pestaña **Actions**.
2. Si aparece un botón para habilitar los workflows ("I understand my
   workflows, go ahead and enable them"), haz clic.
3. En la lista de la izquierda elige **"Escaneo de segmentos sin nombre"**.
4. Botón **Run workflow** → en "Modo de escaneo" elige **`test`** → **Run workflow**.
5. Espera unos 2–3 minutos (se pone una palomita verde ✅ al terminar).
6. Abre tu página y recárgala: deberías ver los segmentos sin nombre del
   **centro de Guadalajara** (la zona de prueba). Prueba un permalink:
   debe abrir el WME justo en ese segmento.

### Si la prueba salió bien → escaneo completo

- Vuelve a **Actions → Run workflow**, ahora con modo **`completo`**, o
  simplemente no hagas nada: todos los días a las **03:17** (hora de CDMX)
  correrá solo.
- México es enorme (~14,600 celdas): cada corrida diaria de 4 horas avanza una
  parte y guarda por dónde va. La **primera vuelta completa al país tarda
  varios días**; el avance se ve en la tarjeta "Cobertura del escaneo" de la
  página. Después de la primera vuelta, sigue dando vueltas para mantener los
  datos frescos.

---

## Preguntas frecuentes

**¿Puedo escanear solo mi zona para que sea más rápido?**
Sí. Edita `scanner/config.json` (en GitHub: abre el archivo → ícono de lápiz)
y cambia `bbox` por las coordenadas de tu zona
`[lon_oeste, lat_sur, lon_este, lat_norte]`. Borra también el archivo
`state/scan_state.json` para que empiece de cero.

**¿Cómo cambio la hora o frecuencia?**
Edita `.github/workflows/escaneo.yml`, línea del `cron`. Está en hora UTC
(CDMX = UTC−6). Ejemplo: `'17 9 * * *'` = 03:17 en CDMX.

**¿Y si algo falla?**
- En **Actions** puedes abrir la corrida y ver el registro (log).
- Si la página muestra un aviso ⚠️ de acceso, copia el contenido del archivo
  `state/debug_ultimo_error.txt` del repositorio y compártemelo en el chat de
  Claude para ajustar el escáner.

**¿Esto le pega duro a Waze?**
No: hace peticiones con pausa (≈3 por segundo máximo), igual que un editor
navegando el mapa, y usa la misma vía pública del modo práctica.
