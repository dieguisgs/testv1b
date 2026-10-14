# Cómo entender el backtest de VWAPSb

El backtest plantea una pregunta que sí podemos comprobar: **si este precio propio conocido hubiera faltado, ¿qué habría calculado el algoritmo con la información restante disponible?** El precio propio ocultado es la respuesta de referencia. EEX aporta información; no es el valor que obligatoriamente debe copiar la estimación.

Esto aporta evidencia sobre huecos aislados. No demuestra que los precios desconocidos sean correctos, que un VWAP negociado sea exactamente el valor teórico del mercado ni que una zona entera sin datos se rellene igual de bien. Los datos sintéticos comprueban el funcionamiento; para medir precisión real hacen falta observaciones reales.

## 1. El examen, explicado de forma sencilla

Imagina que un producto tiene M+1 = 100, M+2 = 110 y Q+1 = 120 en una fecha.

1. Ocultamos M+1, mantenemos M+2 y Q+1, calculamos M+1 y comparamos con 100.
2. Restauramos M+1 y ocultamos M+2. Lo calculamos y comparamos con 110.
3. Restauramos M+2 y ocultamos Q+1. Lo calculamos y comparamos con 120.

Son **tres exámenes independientes**, no una eliminación acumulativa de los tres precios. Las reglas del algoritmo siguen vigentes: que Q+1 permanezca visible no permite usarlo como ancla LOCAL para M+1, porque esta versión solo acepta anclas de la misma familia.

Por defecto se examinan todos los casos elegibles. Diez contratos durante veinte fechas producen 200 casos por combinación si son contratos físicos distintos. Los precios propios que ya faltan no tienen una respuesta conocida con la que comparar y, por tanto, no se puntúan.

## 2. Qué es exactamente un caso

Un caso identifica una fecha de referencia, la identidad exacta `(product, region, unit)` y un contrato físico de entrega. Son elegibles las observaciones propias finitas con tenor reconocido y mapping activo; se excluyen periodos sin horas de entrega aplicables. La lista se construye **antes de aplicar los filtros del candidato**. Subir el volumen mínimo no puede borrar del examen una respuesta difícil.

Se ocultan juntas las etiquetas equivalentes y las filas duplicadas. Por ejemplo, `M+1` y `M+01` no pueden dejar una copia visible de la respuesta. Las horas efectivas forman parte de la equivalencia: compartir límites de entrega no convierte automáticamente en iguales contratos con distintas convenciones de horas Peak.

Las observaciones duplicadas se consolidan igual que en el motor:

- Si hay volúmenes positivos y finitos, se calcula la media ponderada de esas filas. Las filas con volumen cero, negativo o desconocido no contribuyen a esa media ponderada.
- Si ninguna fila tiene volumen positivo, se utiliza la media simple de los precios finitos.

Con precio 110 y volumen 1, precio 130 y volumen 3, y precio 999 y volumen 0, la respuesta ocultada es `(110 × 1 + 130 × 3) / 4 = 125`. Sin ningún volumen positivo, los precios 110 y 130 darían una media simple de 120.

También pueden examinarse contratos propios reconocidos que no estén en la lista de tenors objetivo. Para ese caso se añade temporalmente la etiqueta ocultada a los objetivos de predicción. Por eso conviene revisar los resultados por familia y no asumir que el agregado representa únicamente los tenors objetivo.

## 3. Cuántos precios se ocultan

`mask_fraction=1.0` examina todos los casos. `mask_fraction=0.5` selecciona aproximadamente la mitad, redondeando hacia arriba y con un mínimo de un caso. `seed` permite repetir la misma selección. La selección se realiza una sola vez y es idéntica para todos los candidatos.

**La fracción determina cuántos exámenes independientes hacemos; no cuántos precios se ocultan simultáneamente.** Cada examen sigue ocultando un solo contrato físico, incluidas sus etiquetas equivalentes y filas duplicadas.

Actualmente no se simulan varios meses consecutivos ausentes, un día entero sin observaciones propias ni una ausencia persistente durante varias fechas. Esos escenarios requieren otros patrones de ocultación. El resultado de este examen de huecos aislados no debe presentarse como validación de esos otros casos.

## 4. Fechas, histórico y disponibilidad de EEX

`start` y `end` delimitan las fechas evaluadas, ambas incluidas. Los datos propios anteriores siguen disponibles para el histórico, dentro de las reglas de cada candidato. Las filas propias futuras se eliminan antes de llamar al motor. Solo se oculta el caso de la fecha actual; sus observaciones genuinas anteriores pueden seguir ayudando.

La disponibilidad de EEX respeta las mismas reglas que producción:

- Desplazamiento `0`: última publicación disponible hasta la propia fecha.
- Desplazamiento `-1`: última publicación disponible hasta el día natural anterior.
- Desplazamiento `-2`: última publicación disponible hasta dos días naturales antes.

Un lunes con `-1` puede utilizar el viernes. La antigüedad se mide desde la fecha de referencia. Los pares propios/EEX históricos exigen publicación EEX de su misma fecha, deben ser anteriores al día calculado y estar disponibles según su límite temporal. El contrato objetivo se sigue definiendo según el día calculado, aunque la publicación EEX sea anterior.

Los archivos contienen fechas de publicación, no todas las versiones y horas históricas de publicación o revisión. Por tanto, esto representa una disponibilidad por fecha; no demuestra qué versión revisada podía verse en un minuto concreto del pasado.

## 5. Malla de candidatos y configuración por producto

La malla es un diccionario con listas de valores:

```python
PARAMETER_GRID = {
    "local_max_steps": [1, 2],
    "history_enabled": [False, True],
}
```

Este ejemplo genera cuatro combinaciones. Un diccionario vacío evalúa una vez la configuración efectiva actual. Solo se admiten los diez campos de `Model`, con un máximo de 256 candidatos. Se rechazan valores inválidos y combinaciones completas incompatibles.

La base de cada producto es su configuración efectiva de producción: los valores globales en modo global, o los globales más sus excepciones fijas del mapping en modo individual. **Los valores incluidos en la malla sobrescriben esa base durante el experimento.** Los parámetros ausentes de la malla conservan sus valores por producto. Así, un mismo candidato puede tener parámetros no explorados diferentes entre productos si la producción está en modo individual. `parameters` guarda el modelo efectivo completo de cada caso; `grid_parameters` guarda los valores compartidos de ese candidato.

El experimento no reescribe el mapping ni adopta un candidato automáticamente. Este flujo sencillo no elige ganadores independientes por producto ni ejecuta mallas diferentes por producto. Puedes revisar los resultados de cada uno o preparar otra ejecución con una configuración y mapping deliberadamente restringidos.

## 6. Qué mide cada métrica

Imagina tres precios reales ocultados: 100, 120 y 140. Un candidato calcula 102, 116 y deja el tercero sin precio.

| Métrica | Cálculo | Resultado |
|---|---|---:|
| Casos | Todos los contratos seleccionados | 3 |
| Predichos | Predicciones finitas | 2 |
| Sin predicción | Casos sin precio calculado | 1 |
| Cobertura | 2 / 3 | 66,7 % |
| Errores | Predicción menos real | +2, -4 |
| MAE | (2 + 4) / 2 | 3 |
| RMSE | Raíz de ((4 + 16) / 2) | 3,162 |
| Sesgo | (2 - 4) / 2 | -1 |

El MAE expresa el error absoluto medio en la unidad del precio. El RMSE da más importancia a los errores grandes. Un sesgo positivo indica sobreestimación media y uno negativo indica subestimación. Si no hay ninguna predicción, la cobertura es cero y los errores quedan vacíos: no se inventa un error cero.

`summary.csv` agrupa por candidato y unidad. Nunca se mezclan errores EUR/MWh y GBP/MWh. Dentro de una misma unidad, cada caso pesa igual: un producto con más observaciones aporta más casos. `by_curve.csv` separa además la identidad exacta y la familia del contrato; sirve para detectar productos o tramos que el agregado pueda ocultar.

## 7. Cómo comparar candidatos con criterio

No hay **ganador automático, penalización prefijada por hueco ni una puntuación combinada**. Un MAE inferior con menor cobertura no significa automáticamente que el candidato sea mejor: puede haberse abstendido en los casos más difíciles.

Otro candidato podría calcular los tres precios del ejemplo como 101, 119 y 160. Su cobertura sería 100 % y su MAE, 7,333. Sin embargo, en los dos primeros casos compartidos tendría un MAE de 1, mejor que el 3 del primer candidato. La predicción adicional tiene un error de 20. Hay que decidir si esa estimación adicional es útil para tu objetivo; una media aislada no resuelve esa decisión.

Cuando las coberturas difieran, utiliza `scores.csv` para comparar también los casos que ambos candidatos predicen, conservando el informe de cobertura sobre todos los casos originales. Esta comparación común es un análisis manual en esta versión; no se modifica silenciosamente la muestra usada en las métricas publicadas. Revisa productos, familias, errores grandes, abstenciones y la forma de la curva completa. La coherencia entre contratos solapados es otro diagnóstico y no forma parte del MAE.

## 8. Fechas para explorar y fechas para comprobar

Esta versión **no separa automáticamente calibración y validación ni elige ganador**. Mide las fechas que le indiques.

Un procedimiento razonable es explorar combinaciones en un intervalo anterior, elegir y fijar los parámetros y ejecutar esos valores fijos sobre fechas posteriores que no hayas utilizado para decidir. El histórico anterior puede seguir aportando la información que tendrías operativamente. Si cambias parámetros después de ver el segundo resultado, esas fechas ya forman parte de la investigación; reserva otro intervalo sin utilizar para la comprobación final.

## 9. Archivos y notebooks

`notebooks/backtest.ipynb` ejecuta la malla y guarda `scores.csv`, `summary.csv` y `by_curve.csv`. Las primeras celdas permiten editar fechas, diccionario, fracción de casos y semilla. `notebooks/grid.example.json` contiene una malla pequeña de ejemplo.

El detalle conserva la trazabilidad normal del motor y añade `candidate_id`, `grid_parameters`, el modelo efectivo `parameters`, el real ocultado `actual`, `error`, `absolute_error`, `squared_error`, `hidden_rows` y `hidden_aliases`. Los casos sin predicción siguen presentes. Su visor enseña predicciones de los casos ocultados; no representa una curva completa rellenada.

`notebooks/inspect_curves.ipynb` abre un refill guardado. Permite seleccionar identidad y fecha, todas las familias juntas o una sola, y medias por intervalos físicos de entrega. Las medias de output y EEX utilizan exactamente las mismas fechas con ambos precios disponibles; las etiquetas equivalentes cuentan una sola vez por día.

## 10. MLflow local opcional

`notebooks/backtest_mlflow.ipynb` añade el servicio local, con puerto 5055 por defecto, y guarda informes y configuración de cada candidato. Instala los extras de notebook y experimentos. El servicio escucha en 127.0.0.1; la última celda puede detener el proceso y sus trabajadores sin borrar los resultados.

Para cada candidato se genera además un **refill completo** de todos los días naturales del intervalo. En esas curvas los precios originales permanecen visibles. El selector de ejecuciones permite abrir esas curvas completas o las predicciones de los casos ocultados. Ambos se cargan desde los artefactos guardados, sin recalcular. La misma carpeta de almacenamiento permite volver a abrir ejecuciones antiguas. El cargador de artefactos no necesita los inputs propios/EEX actuales, aunque la celda habitual de preparación del notebook sí carga su dataset configurado.

MLflow registra y muestra comparaciones. No certifica el objetivo, no elige automáticamente un ganador y no convierte una prueba sintética en evidencia de precisión real. Los artefactos contienen precios y se guardan localmente; utiliza una carpeta apropiada para tus datos reales.
