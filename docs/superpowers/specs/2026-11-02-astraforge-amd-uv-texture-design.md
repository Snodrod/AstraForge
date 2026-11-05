# AstraForge AMD UV Base Color — дизайн

Дата: 2026-11-02

## Цель

Добавить для RX 9070 XT проверяемый single-view режим Pixal3D, который экспортирует GLB с настоящими `TEXCOORD_0`, встроенной PNG-текстурой и `pbrMetallicRoughness.baseColorTexture`. Первый аппаратный результат: draft-геометрия, текстурный latent Pixal3D 1024 и UV-атлас 512 × 512.

Режим не использует CUDA, NATTEN, `nvdiffrast`, CuMesh UV unwrap или новые зависимости. Драйвер AMD, ROCm и системные компоненты не изменяются; перезагрузка не требуется.

## Текущее состояние

AstraForge уже генерирует на AMD геометрию и opt-in `COLOR_0`. Текстурный latent Pixal3D успешно вычисляется на ROCm. В `postprocess_mesh()` полноценный UV-путь сейчас упирается в два несовместимых участка:

1. `cumesh.CuMesh().uv_unwrap()` при отсутствии UV;
2. `dr.RasterizeCudaContext()`, `dr.rasterize()` и `dr.interpolate()` при построении карты позиций.

В установленном ComfyUI уже есть `_uv_unwrap()` и `_rasterize_uv_barycentric()` в `comfy_extras.nodes_mesh_postprocess`, а также `_interp_vertex_attr()`. Они используют Python, NumPy и PyTorch и не требуют NVIDIA-растеризатора.

## Выбранный подход

Повторно использовать родные ComfyUI UV unwrap, tiled UV rasterizer и barycentric interpolation. Остальные этапы Pixal3D сохранить: генерация текстурного latent, `grid_sample_3d`, извлечение атрибутов, OpenCV inpainting, создание `PBRMaterial` и штатный GLB export.

Отклонённые альтернативы:

- CuMesh UV unwrap: оставляет дополнительную нативную ROCm-зависимость и требует отдельной проверки, хотя подходящий ComfyUI-компонент уже доступен.
- Преобразование `COLOR_0` в PNG: создаёт лишь растеризованную копию цветов вершин и не использует полноценную текстурную детализацию Pixal3D.

## Архитектура и поток данных

### AMD patch

`scripts/wsl/patch_trellis2_amd.py` расширяет существующий узкий патч `trellis2_image_to_3d.py`:

1. В AMD-режиме при `bake_on_vertices=False` и отсутствии исходных UV вызывается ComfyUI `_uv_unwrap()` с `segmenter='adaptive'`, разрешением упаковки 1024, `padding=2` и `weld_distance=0.0` (автоматический допуск сварки вершин).
2. Возвращённые `vmapping`, `indices`, `uvs` преобразуются в существующие `vertices_torch`, `faces_torch`, `uvs_torch`, `vertices`, `faces`, `uvs`; нормали переиндексируются через `vmapping`.
3. Вместо nvdiffrast вызываются `_rasterize_uv_barycentric()` и `_interp_vertex_attr()` для получения `mask` и интерполированных 3D-позиций каждого покрытого текселя.
4. Дальше выполняется существующий `grid_sample_3d` и общий код материала. Ориентация вершин, нормалей и V-координаты сохраняется без изменений.

Патч активен только при `ASTRA_AMD_ROCM=1`. Windows NVIDIA-путь и режим `bake_on_vertices=True` не меняются.

### Workflow и API

Добавляется булево поле `uvTexture`, по умолчанию `false`.

- `uvTexture=true` разрешено только для AMD WSL single-view workflow.
- `uvTexture=true` и `vertexColors=true` взаимоисключающи.
- Узел `Trellis2MeshTexturingPixal3DMultiView` использует те же параметры текстурного latent, что проверенный `COLOR_0`, но `bake_on_vertices=false` и `texture_size=512`.
- Shape-only остаётся режимом по умолчанию; существующий `COLOR_0` сохраняется.

Аппаратный runner получает `AMD_PIXAL3D_UV_TEXTURE=1` и пишет отдельный `data/runtime/amd-pixal3d-single-uv.glb`, не перезаписывая прежние результаты.

## Проверка результата

`validateGeneratedMesh()` получает опцию `requireUvTexture`. Для каждого triangle primitive требуются:

- валидные `POSITION` и индексы, как прежде;
- `TEXCOORD_0` типа `VEC2` с поддерживаемым component type и количеством, совместимым с позициями;
- материал с `pbrMetallicRoughness.baseColorTexture`;
- texture reference на существующий image;
- PNG, встроенный через `bufferView`, с ненулевой длиной и корректной сигнатурой.

Проверка выполняется до и после нормализации GLB. Нормализация позиций не должна изменять UV, images, textures или material references.

Дополнительная внешняя проверка:

- атлас декодируется как PNG и содержит неоднородные RGB-значения;
- UV покрытие ненулевое;
- GLB рендерится с четырёх сторон с фактической base color texture;
- результат добавляется отдельной сценой в существующий AMD preview.

## Ошибки и ограничения

- Отсутствие UV, пустое UV-покрытие, отсутствие texture/image или повреждённый PNG вызывают явную ошибку. Скрытого fallback к `COLOR_0` или shape-only нет.
- Ошибка ROCm OOM возвращается существующим понятным сообщением; драйвер и runtime не переустанавливаются.
- Первый прогон ограничен single-view и атласом 512 × 512. Multi-view UV, 1024-атлас, 1024-геометрия с UV и дополнительные PBR-карты не входят в критерий завершения этой задачи.
- Metallic/roughness сохраняются штатным upstream-кодом, но аппаратный критерий этой задачи проверяет именно base color texture.

## Тесты

1. Статические/unit-тесты AMD patch: AMD UV unwrap и rasterizer выбраны, nvdiffrast и CuMesh UV не вызываются в AMD-ветке; NVIDIA и `COLOR_0` anchors сохранены.
2. Workflow tests: defaults не меняются, UV opt-in имеет `bake_on_vertices=false`, `texture_size=512`, multi-view и конфликт с `vertexColors` отклоняются.
3. GLB fixtures: корректный UV/PNG проходит до и после normalize; отсутствующие UV, texture, image и повреждённый PNG отклоняются.
4. E2E option tests: отдельный opt-in, запрет неподдерживаемых сочетаний и отдельное имя результата.
5. Полный `npm test`, `py_compile`, `git diff --check`.
6. Реальный AMD E2E на RX 9070 XT с записью job id, времени, VRAM, геометрии и размера GLB; затем визуальный просмотр атласа и четырёх рендеров.

## Commit и доставка

Runtime, веса, логи, GLB и PNG не добавляются в Git. Исходники, тесты и документация коммитятся в `amd-rocm` и отправляются в существующий PR #1 только после успешной аппаратной и визуальной проверки. Merge PR не выполняется.
