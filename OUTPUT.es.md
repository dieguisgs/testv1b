# Diccionario del output de VWAPSb

[English](OUTPUT.md) · [README](README.es.md)

Los comandos de producción escriben `filled.csv`, `enriched.csv`, `consistency.csv` y `run_manifest.json` en `[paths].output_dir`. Cada fichero responde a una pregunta: la curva canónica, el input original con información del relleno, la coherencia entre entregas y la configuración de la última ejecución.

## 1. Curvas canónicas: `filled.csv`

Cada fila identifica una fecha de referencia, una identidad exacta `(product, region, unit)` y un tenor. Los precios están expresados en la `unit` de la fila y las fechas en `YYYY-MM-DD`. La entrega es el intervalo **[delivery_start, delivery_end)**: incluye el inicio y excluye el final.

El motor genera los tenors objetivo configurados y los tenors originales reconocidos de ese día para los productos habilitados. Un objetivo relativo que no pueda resolverse a un periodo de entrega no genera fila. Por ejemplo, BOW/BOM pueden dejar de existir al terminar el periodo correspondiente. Contar filas del CSV no basta para deducir la cobertura de todos los contratos posibles.

| Columna | Valores e interpretación |
| --- | --- |
| `date` | Fecha de referencia para la que se calcula la curva. Puede ser distinta de la fecha de publicación EEX. |
| `product` | Identificador exacto del producto según mapping/input. |
| `region` | Identificador exacto de región; puede estar vacío si así se define la identidad. |
| `unit` | Identificador exacto de unidad, por ejemplo `EUR/MWh`. No se deben mezclar precios de unidades distintas. |
| `tenor` | Etiqueta relativa, por ejemplo `D+1`, `M+1`, `Q+2`, `Cal+1`. Debe interpretarse junto con `date` y las fechas de entrega. |
| `kind` | Familia resuelta: `Day`, `Weekend`, `Week`, `Month`, `Quarter`, `Season`, `Year`, `BOW` o `BOM`. |
| `delivery_start` | Primer día de entrega, incluido. |
| `delivery_end` | Primer día fuera de la entrega, excluido. |
| `price` | Precio final finito, o vacío si no se ha conseguido un precio. Cero y los precios negativos son válidos. |
| `status` | `original`, `estimated` o `missing`; se explica en la tabla de métodos. |
| `method` | `original`, `own_reused`, `local`, `history`, `eex_ewma`, `eex_simple` o `missing`. |
| `eex_price` | Referencia EEX del objetivo en la publicación seleccionada, si existe. Puede ser un contrato exacto, un agregado de contratos menores o un residual. **No** es la media móvil de EEX. |
| `eex_date` | Publicación EEX seleccionada para esa curva y fecha de referencia. Puede estar informada aunque ese objetivo concreto no tenga referencia EEX. |
| `eex_cutoff` | Última fecha permitida de publicación: `date + eex_offset_days`. `-1` retrocede un día natural el límite de información; no desplaza la entrega. |
| `eex_age_days` | Días naturales entre `date` y `eex_date`; vacío si no se seleccionó ninguna publicación. |
| `adjustment` | Ajuste aditivo propio menos EEX empleado por `local` o `history`, en la misma unidad que el precio. Vacío en otros métodos. |
| `local_count` | Número de anclas LOCAL admisibles encontradas si se alcanzó esa etapa. Puede ser positivo pero insuficiente, aunque finalmente se elija otro método. También es cero cuando LOCAL no llegó a evaluarse. |
| `history_days` | Número de fechas propias históricas distintas admisibles encontradas si se alcanzó HISTORY. Puede ser positivo pero inferior a `history_min_days`. Cero no demuestra que el input no tenga histórico: una etapa anterior puede haber resuelto el punto. |
| `history_start` | Primera fecha histórica admisible encontrada; vacío si no se encontró ninguna o no se evaluó el histórico. Puede estar informada aunque HISTORY no alcanzara el mínimo. |
| `history_end` | Última fecha histórica admisible encontrada, con la misma interpretación. No es la última fecha existente en el input. |
| `reason` | Motivo final de selección o fallo, enumerado más abajo. Los fallos de etapas previas quedan en `details`. |
| `details` | Objeto JSON con configuración efectiva, decisiones y evidencia del cálculo. Hay que interpretarlo como JSON, no separarlo por comas. |
| `configuration_id` | Identificador SHA-256 determinista de 64 caracteres de la configuración efectiva de esa curva y sus ajustes relevantes de mapping/calendario. No es una huella de los datos de entrada ni demuestra que dos ejecuciones usaran los mismos ficheros. |

### Estado, método y motivo final

Se intenta cada etapa en este orden y se termina al conseguir el primer resultado. En VWAPSb no existe una mezcla de LOCAL e HISTORY.

| `status` | `method` | Significado | `reason` cuando funciona |
| --- | --- | --- | --- |
| `original` | `original` | Existe una observación propia finita para esa etiqueta. La curva canónica puede agregar observaciones duplicadas. | `observed_original_period` |
| `original` | `own_reused` | Existe un precio propio con otra etiqueta para el mismo intervalo y las mismas horas de entrega. Reutiliza un observado; no extrapola un contrato cercano. | `equivalent_original_delivery_period` |
| `estimated` | `local` | EEX actual del objetivo más la mediana de las diferencias aditivas de anclas propias admisibles de la misma familia y fecha. | `same_kind_local_median` |
| `estimated` | `history` | EEX actual del objetivo más la mediana de las medianas diarias históricas de diferencias propio menos EEX. | `historical_median_of_daily_medians` |
| `estimated` | `eex_simple` | Media aritmética de la ventana exigida de publicaciones EEX, siguiendo siempre la entrega física fija del objetivo. | `complete_fixed_contract_eex_window` |
| `estimated` | `eex_ewma` | Media exponencial normalizada de esa ventana completa del contrato fijo. Los datos recientes pesan más. | `complete_fixed_contract_eex_window` |
| `missing` | `missing` | Ninguna etapa aplicable produjo un precio, o el intervalo tiene cero horas de entrega. | Uno de los motivos de fallo siguientes. |

Los motivos finales de ausencia son `zero_delivery_hours`, `no_eex_book`, `no_publication_within_cutoff_and_age`, `insufficient_publications`, `missing_contract_in_window` y `degenerate_averaging_weights`. El último indica que el redondeo numérico dejó menos de dos pesos positivos; el motor no sustituye silenciosamente la media por un único dato EEX.

Ejemplo: EEX objetivo `120` y diferencias LOCAL aceptadas `-5`, `3`, `7` producen `adjustment=3`, `price=123`, `status=estimated`, `method=local`, `local_count=3`. `eex_price` sigue siendo `120` para comprobar la cuenta.

Una media EEX puede coincidir numéricamente con el último settlement si todos los precios de la ventana son iguales. Esa igualdad no implica sustitución directa por EEX: los datos y pesos de `details` permiten distinguir el cálculo.

### Duplicados y etiquetas equivalentes

Si varias observaciones propias representan el mismo intervalo y las mismas horas de entrega, el precio canónico es la media ponderada por los volúmenes positivos cuando existe al menos uno. En ese caso, las observaciones sin volumen positivo no participan en esa media. Si ninguna tiene volumen positivo, se utiliza la media aritmética. `details.own.observations` conserva las observaciones de origen para inspeccionarlas.

El motor también evita estimar valores diferentes para etiquetas objetivo equivalentes. Calcula un representante determinista y reutiliza ese resultado para las demás. Cada fila conserva su `tenor`/`kind` e incorpora `details.equivalent_target` con la etiqueta representante, su familia y la regla `same_delivery_interval_and_hours`. Se conserva el método de estimación elegido; esta reutilización no se convierte en `own_reused` salvo que el origen sea un observado propio.

## 2. Cómo leer `details`

Cada fila incluye `settings`, `eex_reference_method` y `stages`. Un valor JSON ausente aparece como `null`; una lista vacía indica que no se registraron elementos.

* `settings.model`: los diez parámetros efectivos del modelo, incluidos valores heredados y ajustes individuales. `settings` también contiene objetivos, modo de configuración, disponibilidad EEX, convenciones del calendario y mapping. Es la fotografía de configuración de esa fila.
* `eex_reference_method`: `exact`, `strip`, `residual` o `null`. `strip` combina una cobertura completa de contratos menores ponderando por horas de entrega. `residual` obtiene la parte pendiente a partir de un contrato mayor y sus partes conocidas. Describen cómo se obtiene la **referencia EEX**, no otro método de relleno de precios propios.
* `stages`: decisiones en orden de ejecución. Las etapas posteriores a un resultado seleccionado no aparecen porque no se evaluaron.
* `own`, cuando corresponde: etiquetas propias, periodo físico, precio, volumen, observaciones y regla de agregación.
* `equivalent_target`, cuando corresponde: representante de las etiquetas estimadas equivalentes explicado antes.

| Etapa | Evidencia y resultados posibles |
| --- | --- |
| `original` | `selected`, `equivalent_alias`, `unavailable` o `zero_delivery_hours`. |
| `local` | `required_anchors`, anclas aceptadas en `anchors` y rechazadas en `rejected`. Resultados: `disabled`, `target_eex_unavailable`, `insufficient_anchors`, `selected`. Las aceptadas incluyen precios propio/EEX, método de referencia EEX, diferencia aditiva y `distance_steps`. Los rechazos indican `different_kind`, `outside_radius`, `volume_below_minimum_or_unknown` o `missing_eex_reference`. |
| `history` | `window_start`, `required_days`, `target_lead_steps`, `radius_steps`, regla de correspondencia y `days`. Cada fecha incluye sus anclas aceptadas y `daily_median`. Resultados: `disabled`, `target_eex_unavailable`, `insufficient_days`, `selected`. |
| `eex_average` | Número requerido de publicaciones, `average`, `halflife_observations`, entrega fija, `observations` disponibles y `missing_contract_dates`. Cada dato incluye fecha, precio y método de referencia EEX; una ventana completa añade su `weight` normalizado. Resultado: `selected` o uno de los fallos de media EEX enumerados antes. |

LOCAL compara distancias del calendario dentro de la misma familia. HISTORY compara la distancia relativa a entrega del objetivo actual con la distancia relativa de cada observación antigua; no sigue la etiqueta `M+1` como si siempre representara el mismo mes. Cada pareja histórica exige dato propio y EEX en la misma fecha pasada, anterior a la referencia actual y no posterior al límite EEX actual. Cada día admisible aporta una mediana, independientemente de cuántas anclas contenga.

La ventana de media toma las últimas `eex_window` publicaciones hasta la publicación EEX seleccionada. No salta una publicación porque falte ese contrato. Debe existir toda la ventana para esa entrega física fija. La antigüedad y la semivida EWMA se miden en **observaciones publicadas**, no en días naturales: `peso ∝ 2^(-antigüedad_en_observaciones / eex_halflife)`.

## 3. Input original con relleno: `enriched.csv`

Este fichero permite mantener las columnas originales. Conserva el input bruto y añade los 23 campos canónicos con prefijo `refill_`, además de:

| Columna adicional | Valores y significado |
| --- | --- |
| `refill_generated` | `False` para una fila retenida del input; `True` para una fila objetivo añadida porque faltaba en el input. Explica el origen de la fila, no si el precio es estimado. |
| `refill_input_row` | Posición desde uno en el input bruto cargado para filas retenidas; vacío en filas generadas. No incluye la cabecera Excel ni es un identificador permanente entre ficheros/ejecuciones. |

Distinciones importantes:

1. Cada precio propio bruto finito se conserva individualmente en `refill_price`, con `refill_status=original`, `refill_method=original`, `refill_reason="Original input row preserved"` y `refill_details={"retained_input_row":true}`. También se conservan los originales fuera del ámbito de procesamiento. Otros campos de trazabilidad pueden quedar vacíos sin correspondencia canónica. La agregación de duplicados de la curva canónica no sustituye estas observaciones individuales.
2. Una fila sin precio propio finito recibe el resultado canónico correspondiente si existe. Si no existe, queda `refill_price` vacío, `refill_status=missing`, `refill_method=not_processed` y motivo `No matching canonical output row`. `not_processed` solo existe en el enriquecido; no es otro estimador.
3. Una fila generada rellena identidad/fecha/tenor en sus columnas originales y contiene el resultado completo con prefijo. Su columna original de precio queda vacía. El precio completado se lee en **`refill_price`**.
4. Una fila generada puede tener `refill_status=original` y `refill_method=own_reused` si otra etiqueta propia representa exactamente el mismo contrato físico.

Se rechazan inputs que ya contienen columnas `refill_`. Las siguientes ejecuciones deben recibir el input original para evitar que las estimaciones entren como observaciones propias.

## 4. Relaciones entre entregas: `consistency.csv`

Este informe comprueba coberturas completas y sin solapamientos, por ejemplo un trimestre cubierto por tres meses. **Informa de diferencias sin cambiar ningún precio**.

| Columna | Significado |
| --- | --- |
| `date`, `product`, `region`, `unit` | Fecha de referencia e identidad exacta de la curva. |
| `parent_tenor` | Etiqueta del periodo mayor que se comprueba. |
| `parent_price` | Precio final de ese periodo mayor. |
| `children_price` | Media de los periodos menores ponderada por horas de entrega. |
| `difference` | `parent_price - children_price`, en la unidad de la fila. Positivo: el padre está por encima; negativo: por debajo. |
| `children` | Lista JSON con `tenor`, `kind`, fechas de entrega, `price`, `hours` y `weight` normalizado de cada componente. |

La comprobación emplea el calendario de entrega y la zona horaria del mapping y verifica que las horas de los componentes cubran las del padre. Excluye precios ausentes y calendarios de entrega incompatibles. Las etiquetas equivalentes cuentan una sola vez. Si hay varias coberturas completas, la elección es determinista y prioriza el menor número de componentes; no enumera todas las combinaciones posibles.

Si un padre vale `100` y sus componentes completos promedian `103`, `difference=-3`. Valores iguales producen cero, con la precisión numérica correspondiente. Si falta un componente y no existe otra cobertura completa, **no hay fila de auditoría** para ese padre: ausencia no significa coherencia. Este informe mide concordancia interna, no acierto frente a un precio verdadero desconocido.

## 5. Registro de ejecución: `run_manifest.json`

El manifiesto describe **solo la última ejecución**, mientras que los CSV pueden conservar resultados anteriores y otros rangos de fechas. Contiene:

* `schema_version`, `project`, `mode`, `created_utc`: versión del formato, nombre del proyecto, modo de ejecución y fecha/hora UTC.
* `dates`: fechas de referencia solicitadas en esta ejecución.
* `eex_offset_days`, `max_stale_days`, `configuration_mode`: ajustes de la ejecución.
* `rows_written`: filas canónicas nuevas o recalculadas en esta ejecución; `total_rows`: filas canónicas totales tras publicar.
* `curves`: identidad exacta, `configuration_id` y `model` efectivo completo de cada curva activa.
* `note`: recordatorio de que las filas conservadas mantienen su propio identificador de configuración.

El identificador incluye modelo efectivo, objetivos, identidad y ajustes relevantes de EEX/mapping/calendario. Excluye rutas como la raíz del proyecto o la carpeta de salida, de modo que copiar el proyecto no cambia por sí mismo el identificador. No calcula huellas del contenido de los datos ni de la versión del código. Catchup comprueba los identificadores guardados dentro del ámbito solicitado y rechaza configuraciones incompatibles; para recalcular ese ámbito hay que ejecutar refill explícitamente.

## 6. Tablas del backtest

El backtest devuelve las tablas `scores`, `summary` y `by_curve`. El CLI/notebook determina dónde guardarlas. MLflow puede registrar estos resultados; el seguimiento no cambia el significado de sus métricas.

Cada caso oculta las observaciones propias y etiquetas equivalentes de un contrato físico en una fecha de referencia y trata de reconstruirlo con la información restante. Los casos se ocultan **de uno en uno**. `mask_fraction` selecciona una muestra de casos una vez, usando `seed`, y todos los candidatos evalúan esa misma muestra. No elimina simultáneamente ese porcentaje de toda la curva.

La tabla detallada incluye los campos del motor y estos campos de evaluación:

| Columna | Significado |
| --- | --- |
| `candidate_id` | Etiqueta local de la búsqueda, como `candidate_001`; no es un identificador estable entre búsquedas. |
| `grid_parameters` | JSON de los valores explícitos del candidato en la malla. |
| `parameters` | JSON de los diez parámetros efectivos para esa curva y ese candidato. |
| `actual` | Precio propio ocultado y agregado según la misma regla de duplicados del motor. |
| `error` | `price - actual`; positivo indica sobreestimación. Vacío si no hubo predicción. |
| `absolute_error` | Valor absoluto del error. |
| `squared_error` | Error al cuadrado, en unidades de precio al cuadrado. |
| `hidden_rows` | Número de observaciones propias retiradas para ese caso; no son días históricos ni candidatos. |
| `hidden_aliases` | Lista JSON de las etiquetas ocultadas para esa entrega física. |

El resumen agrupa por `candidate_id` y `unit`. El informe por curva agrupa por candidato, identidad exacta y `kind`. Ambos incluyen `grid_parameters` y:

| Métrica | Significado |
| --- | --- |
| `n_cases` | Contratos ocultados seleccionados para evaluar, incluidos los que no se consiguen predecir. |
| `n_predicted` | Casos con predicción finita. |
| `n_missing` | Casos sin predicción: `n_cases - n_predicted`. |
| `coverage` | `n_predicted / n_cases`, entre 0 y 1; no es un texto con porcentaje. |
| `mae` | Error absoluto medio de los casos predichos, en unidades de precio. |
| `rmse` | Raíz del error cuadrático medio de los casos predichos, en unidades de precio; penaliza más los errores grandes. |
| `bias` | Error con signo medio de los casos predichos. |

Si no se predice ningún caso, la cobertura es cero y los errores quedan vacíos, no son cero. Hay que comparar error **y** cobertura: rechazar casos difíciles puede mejorar aparentemente el MAE. Las unidades se mantienen separadas y todos los candidatos usan los mismos casos seleccionados. Estas pruebas miden recuperación de precios observados; no demuestran que los huecos reales tengan la misma dificultad o distribución. Una búsqueda no instala automáticamente una configuración ganadora en producción.
