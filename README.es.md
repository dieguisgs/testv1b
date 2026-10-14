# VWAPSb

[English](README.md) · [Algoritmo](ALGORITHM.es.md) · [Backtest](BACKTEST.es.md) · [Columnas del output](OUTPUT.es.md)

Una alternativa independiente y más sencilla a VWAPS para completar curvas de **electricidad**. Conserva las observaciones propias y elige un solo procedimiento para cada hueco: observaciones próximas de hoy, diferencias históricas entre precios propios y EEX o una media de publicaciones EEX. Nunca utiliza una cotización EEX sin transformar como último recurso.

Tiene **10 parámetros del modelo**. Se eliminan ratio/auto, mezcla LOCAL+HIST, correlación, estimación entre productos, arbitraje con precios propios y ajuste de forma. La coherencia mes/trimestre/año se comprueba, pero no se fuerza. Esto facilita entender las decisiones; no demuestra que prediga mejor. Los ejemplos son sintéticos: no se ha podido validar con tus datos reales de empresa.

## Instalación en este ordenador o en otro

Requiere Python **3.11 o posterior**. Desde la carpeta del proyecto en PowerShell:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -e ".[dev,notebook,experiment]"
.venv\Scripts\python.exe -m ipykernel install --user --name vwapsb --display-name "VWAPSb"
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe run.py demo
```

Si tienes otra versión compatible, cambia `py -3.11`. En Linux/macOS usa `python3 -m venv .venv` y `.venv/bin/python`. No hace falta ningún archivo `.env`. En VS Code selecciona el kernel **VWAPSb** o el intérprete `.venv` de este proyecto. Las dependencias tienen que instalarse en el mismo entorno que ejecuta el notebook.

Para reproducir las versiones resueltas que acompañan al repositorio, usa `uv sync --all-extras --frozen` con el `uv.lock` incluido y después `uv run python run.py demo` o el intérprete `.venv`. Los comandos pip anteriores instalan versiones compatibles, pero no reproducen ese lockfile.

La demo genera datos sintéticos en `output/demo/` y calcula del 21 de septiembre al 9 de octubre de 2026. Incluye días, fines de semana, semanas, meses, trimestres, temporadas y años. No cambia la configuración de datos reales. Repetir `demo` regenera únicamente sus archivos sintéticos.

## Conectar los datos reales

1. Copia `config.toml` a `config.local.toml` en la raíz del proyecto y edita las rutas. Las relativas parten de **la carpeta del TOML**, independientemente de dónde ejecutes Python.
2. Indica en `[paths].vwap_input` el CSV/XLSX/XLSM original o un patrón que incluya el histórico propio acumulado. En `eex_curves_dir`, la carpeta `output/curves/POWER` del scraper. Aquí no se incluyen scraper, credenciales ni acceso a datos de mercado.
3. Adapta `[vwap_columns]` si tus nombres difieren. Los predeterminados son `reference_date`, `product`, `region`, `unit`, `tenor2`, `vwap`, `total_volume`. El volumen es opcional. Las fechas deben ser legibles; se admiten decimales con coma. No se interpretan separadores de miles ni se convierten divisas. Convierte los `.xls` antiguos a XLSX.
4. Genera el mapping, revísalo y activa las filas elegidas:

```powershell
.venv\Scripts\python.exe run.py mapping --config config.local.toml
.venv\Scripts\python.exe run.py check --config config.local.toml --date 2026-10-09
```

La identidad es **product + region + unit**, exactamente igual tras quitar espacios exteriores. Una región/unidad vacía es un valor vacío, no un comodín. Solo se procesan filas con `use=fill` y `eex_file` informado. Los borradores nacen en `off`. El programa no convierte monedas ni garantiza que una asignación manual sea económicamente correcta: revisa unidades, zona horaria y perfil Base/Peak/Peak7.

El mapping admite CSV o XLSX; en Excel se lee la primera hoja. Para crearlo como Excel, pon `[paths].mapping = "mappings/products.xlsx"` antes de ejecutar `mapping`. Hay un ejemplo de columnas en `products.example.csv`. Los diez parámetros del modelo pueden ser columnas del Excel. `run.configuration_mode="global"` usa el TOML; `"individual"` aplica los valores no vacíos de cada producto. Las celdas vacías heredan el global. `false` es un valor, no una celda vacía. Las columnas desconocidas provocan un error claro.

EEX debe venir en el CSV largo del scraper: `tradeDate`, `maturityType`, `deliveryStart`, `settlPx`. Tipos admitidos: `Day`, `Weekend`, `Week`, `Month`, `Quarter`, `Season`, `Year`. Se ignoran columnas extra. No uses el CSV ancho. La ausencia de archivos se registra; un archivo presente pero mal formado detiene el proceso. El proyecto no descarga settlements.

Ejemplo para el otro ordenador:

```toml
[paths]
vwap_input = "D:/MarketData/own/*.xlsx"
mapping = "mappings/products.xlsx"
eex_curves_dir = "D:/MarketData/eex_scraper/output/curves/POWER"
output_dir = "output/company"
```

Los datos reales, la configuración local y los resultados quedan fuera de Git. Los archivos externos se leen sin modificarlos.

Para pasar desde VWAPS: se mantienen los formatos de input propio/EEX, pero utiliza la plantilla **nueva** de configuración. Puedes reutilizar las columnas de identidad, archivo EEX y calendario del mapping anterior; elimina los parámetros antiguos que ya no existen y cambia filas `helper` a `off`, salvo que quieras rellenar esa curva. Los outputs y configuraciones antiguas no son intercambiables con los de este proyecto independiente.

## Diario, histórico y pendientes

```powershell
# Curva de hoy. Sigue necesitando acceso al histórico propio.
.venv\Scripts\python.exe run.py daily --config config.local.toml

# Día elegido, excluyendo la publicación EEX del propio día.
.venv\Scripts\python.exe run.py daily --config config.local.toml --date 2026-10-09 --eex-offset -1

# Recalcular todas las fechas de referencia del intervalo, incluidos extremos.
.venv\Scripts\python.exe run.py refill --config config.local.toml --start 2026-09-01 --end 2026-10-09

# Añadir filas ausentes y reintentar precios missing; conservar los ya calculados.
.venv\Scripts\python.exe run.py catchup --config config.local.toml --start 2026-09-01 --end 2026-10-09
```

`daily` usa por defecto la fecha local del ordenador. `refill`, primera y última fecha del input. `catchup`, desde la primera fecha del input hasta ayer. Para automatizar, es preferible indicar fechas explícitas. Los rangos incluyen fines de semana. No queda un servicio ejecutándose: puedes programar el comando diario en Task Scheduler/cron.

`offset_days=0` permite EEX de hoy si existe; si no, la última publicación anterior. `-1` permite como máximo ayer; `-2`, anteayer. Son **días naturales**, no posiciones dentro de las sesiones de mercado. No se desplaza el periodo de entrega de M+1 o Q+1. El log y el output indican qué publicación se utilizó realmente. `max_stale_days=7` rechaza una publicación de más de siete días naturales respecto a la fecha de referencia; `0` elimina ese límite de antigüedad.

Salidas: `filled.csv`, una fila canónica por fecha/identidad/tenor; `enriched.csv`, todas las celdas originales más columnas `refill_` y filas objetivo que faltaban; `consistency.csv`, comprobaciones de agregación completa; `run_manifest.json`, información de la última ejecución; `logs/`, registro. Repetir daily/refill reemplaza su ámbito sin duplicar filas. Se conservan otras fechas/productos. Catchup rechaza cambios de configuración dentro del intervalo solicitado: usa refill al modificar parámetros, corregir observaciones o cambiar tenors objetivo. No detecta revisiones del input. La publicación maneja fallos ordinarios con recuperación; no es una transacción indivisible ante un corte eléctrico.

## Ver y comparar resultados

`notebooks/inspect_curves.ipynb` permite seleccionar curva/fecha, ver todos los tenors y calcular medias por rango. `notebooks/backtest.ipynb` permite editar fechas y una malla explícita de parámetros. Están en inglés y tienen ejemplos de rutas para el otro ordenador. Ejecuta primero la demo para ver resultados aquí.

También puedes lanzar:

```powershell
.venv\Scripts\python.exe run.py backtest --config output/demo/config.toml --start 2026-10-01 --end 2026-10-09 --grid notebooks/grid.example.json
```

`notebooks/backtest_mlflow.ipynb` añade un servicio MLflow local y registro de experimentos, con fechas y malla editables. El notebook básico sigue funcionando sin MLflow. Ninguna configuración se publica automáticamente como ganadora. [BACKTEST.es.md](BACKTEST.es.md) explica por qué hay que mirar cobertura y error juntos y por qué ocultar huecos aislados no demuestra precisión en todos los desconocidos reales.

Los diez parámetros y todas las opciones operativas se explican en [ALGORITHM.es.md](ALGORITHM.es.md). Empieza verificando mapping y fechas, y después compara una malla pequeña. Los valores iniciales no están calibrados para tus productos. Esta versión siempre usa diferencias aditivas: no tiene modo ratio.
