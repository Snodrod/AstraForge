# Orc Neutral Rig: рабочий риг для ручной доводки

Дата: 2026-11-05.

## Назначение

Отдельный вариант орка с более собранной стойкой, повёрнутой в нужную сторону головой и структурой Blender, пригодной для дальнейшей ручной настройки весов и поз. Исходная Light-модель и прежний диагностический Rig draft не заменяются.

Это **не production-ready анимационный риг**. Жёсткие проверки весов и площади разрывов пройдены, но остаются предупреждения по единичным рёбрам и самопересечениям. IK, готовые анимационные клипы и художественная приёмка деформации не входили в этот этап.

## Исходники и неизменность Light

- Исходный BLEND: `data/runtime/orc-rig-orc-r5/rigged.blend`.
- Исходная Light-модель не изменялась; её SHA-256 остаётся `60146E7258A3B3E85089533DC2101EFD7CB18562C4AFEF89008187C4F511C61A`.
- Запрос финального прогона: `data/runtime/orc-rig-orc-refine-r2-request.json`.
- Реализация: `scripts/blender/steps/rig_orc_refine.py`.
- Интеграционный тест: `scripts/blender/tests/test_orc_rig_refine.py`.

## Воспроизведение

Из корня репозитория `AstraForge-amd`:

```bash
"runtime/blender/blender-5.0.1-windows-x64/blender.exe" -b --factory-startup --python scripts/blender/af_run.py -- rig_orc_refine data/runtime/orc-rig-orc-refine-r2-request.json
```

Интеграционный тест:

```bash
ORC_RIG_INTEGRATION=1 "runtime/blender/blender-5.0.1-windows-x64/blender.exe" -b --factory-startup --python scripts/blender/tests/test_orc_rig_refine.py
```

Последний наблюдаемый результат теста: `ORC_RIG_REFINE_TEST_PASS`.

## Финальные файлы

- `data/runtime/orc-rig-orc-refine-r2/orc-neutral.blend` — основной файл для ручной работы; SHA-256 `0b2ab3e0387d14978756a7c0cd0c0040ba98c10fac5369b3685b9e582bb32da7`.
- `data/runtime/orc-rig-orc-refine-r2/orc-neutral.glb` — проверенная браузерная версия; SHA-256 `c2f986fb7c566073d4a1aa6c4965b33fd4a9a49af11a6d548d1389eff4ee15b0`.
- `data/runtime/orc-rig-orc-refine-r2/orc-rig-profile.json` — профиль рига.
- `data/runtime/orc-rig-orc-refine-r2/rest.png` — нейтральная стойка с нескольких сторон.
- `data/runtime/orc-rig-orc-refine-r2/poses.png` — контрольные позы.
- `data/runtime/orc-rig-orc-refine-r2/result.json` — метрики и проверки.

В Edit3D используются копии:

- `orc-shape-review/public/models/orc-neutral-rig.glb`;
- `orc-shape-review/blender/orc-neutral-rig.blend`.

## Стойка и структура

- Голова повёрнута на 35°.
- Ноги сведены внутрь на 5°.
- Нейтральная стойка запечена в геометрию и rest pose.
- Объекты: `ORC_RIG`, `ORC_Body`.
- 25 костей, из них 22 deform bones; имена `mixamorig:*` сохранены.
- 9 sockets для крепления эффектов и снаряжения.
- Коллекции: `01 · Core`, `02 · Left limbs`, `03 · Right limbs`, `04 · Ends`.
- В BLEND добавлена инструкция `ORC_START_HERE_RU`.
- Максимум 4 влияния костей на вершину.

Для ручной работы открывать `orc-neutral.blend`, начать с текста `ORC_START_HERE_RU`, затем проверять деформацию на своих целевых позах и локально править веса. Старый `rigged.blend` оставлен как исходная точка для отката.

## Обработка весов

Основной путь использует `welded_smooth_weights` и `motion_group_matrix`, а затем `prepare_orc_skin`:

1. 4 итерации предварительного сглаживания через совпадающие UV/normal-дубли.
2. Разделение анатомических швов через `bmesh.ops.split_edges`.
3. 16 итераций сглаживания внутри анатомических областей.
4. Выбор доминантной motion-группы на контактных вершинах.

Прежнее жёсткое обнуление смешанных весов из основного пути убрано: оно создавало большие растянутые участки. Пороги проверки не ослаблялись.

## Результаты проверки Blender

Финальный `result.json`: `ok: true`.

- 96 960 вершин и 119 998 треугольников в Blender перед GLB-экспортом.
- 744 разделённых boundary edges.
- 0 невзвешенных вершин.
- 0 смешанных весов рука ↔ нога.
- `arms_forward`: `torn_area_frac = 0.00842`, жёсткий лимит `0.01` — пройден.
- `arms_up`: `torn_area_frac = 0.01551`, жёсткий лимит `0.08` — пройден.
- `squat`: `torn_area_frac = 0.00163`.
- `stride`: `torn_area_frac = 0.00092`.
- `p999_stretch = 1.773`, предупреждающий лимит `3.5` — пройден.

Оставшиеся предупреждения:

- `max_edge_stretch = 196.272`, предупреждающий лимит `6` — не пройден; проблема сосредоточена в единичных рёбрах и не описывает типичную деформацию всей поверхности.
- `new_self_intersection_frac = 0.04393`, предупреждающий лимит `0.02` — не пройден; нужна ручная проверка целевых поз и локальная доводка.

## Проверка Edit3D

Сцена `rig-orc` используется для нейтрального рига; сцены `light`, `rig` и `shape` сохранены, сцена по умолчанию остаётся `light`. Первоначальный протокол 1024: `verification/orc-rig-orc/preview-verification.json`.

Первоначально подтверждено в offscreen Edit3D:

- GLB и встроенная base-color текстура 1024 × 1024 загружаются;
- `ORC_Body` является `SkinnedMesh`, скелет содержит 25 костей, в сцене 9 sockets;
- `SkeletonHelper` видим;
- `sceneLint() = []`, `framingReport(...).ok = true`;
- материал проверен через `asset.getMaterials()` и на карточке выбранной модели;
- основной вид и три дополнительных ракурса осмотрены: `verification/orc-rig-orc/contact-sheet.png`;
- масштаб и цвет материала изменяются, undo/redo работают;
- после сохранения и перезагрузки тестовые значения восстановились, затем были возвращены исходные `1` и `#ffffff`;
- финальный `data/edit3d.state.json` содержит пустой `overrides`;
- 47 захваченных сетевых запросов завершились со статусом 200, failed resources не обнаружено.

В браузере GLB содержит 359 896 position-записей при тех же 119 998 треугольниках: экспорт разворачивает индексы по UV/normal/groups. Это не означает увеличение геометрии относительно 96 960 вершин в Blender.

## Обновление base-color до UV 4096

Текущая сцена `rig-orc` называется `Orc rig · нейтральная стойка · UV 4096` и загружает `/models/orc-neutral-rig-uv4096.glb`. Исходные 1024-файлы сохранены для отката.

Новые файлы:

- `data/runtime/orc-rig-orc-uv4096/orc-neutral-uv4096.glb` — SHA-256 `b152ca5e49e92aa626971575c2fa79896e1c9d39362816834d59372e0dff3a46`;
- `data/runtime/orc-rig-orc-uv4096/orc-neutral-uv4096.blend` — SHA-256 `729a48e1031b45dc4fdc1b022b723f2bfa678b9ad2f8251e89fe2fd58a4a3538`;
- `verification/orc-rig-orc-uv4096/basecolor-0-4096.png` — упакованный атлас 4096 × 4096, SHA-256 `28a78f444a8564a123567632e87acad9bae143e9cba0fc220540a63722eb4efe`;
- `data/runtime/orc-rig-orc-uv4096/texture-report.json` — проверка неизменности GLB;
- `data/runtime/orc-rig-orc-uv4096/blend-texture-report.json` — проверка BLEND;
- `verification/orc-rig-orc-uv4096/preview-verification.json` — браузерная проверка.

Атлас получен непосредственно из встроенного 1024-изображения фильтром Lanczos без генеративной дорисовки и перерисовки. Цвет кожи, глаза, клыки, нос, ремень, одежда и отметины сохранены. После обратного уменьшения до 1024 средняя абсолютная ошибка канала составила `0.18039` из 255, PSNR — `53.484 дБ`. Это увеличение существующего атласа, а не создание новой микродетали.

В GLB изменены только base-color image, его bufferView и длина буфера: `asset`, `scenes`, `nodes`, `meshes`, `skins`, `animations`, `accessors` и `samplers` совпадают, исходный бинарный префикс и остальные изображения не изменены. В BLEND только Base Color nodes получили новый упакованный `ORC_BaseColor_4096`; 25 костей, 22 deform bones, 9 sockets, 96 960 вершин и 119 998 полигонов сохранены.

Воспроизведение GLB:

```bash
python scripts/native/upscale_glb_basecolor.py "data/runtime/orc-rig-orc-refine-r2/orc-neutral.glb" "data/runtime/orc-rig-orc-uv4096/orc-neutral-uv4096.glb" --size 4096 --report "data/runtime/orc-rig-orc-uv4096/texture-report.json" --atlas-dir "verification/orc-rig-orc-uv4096"
```

Воспроизведение BLEND:

```bash
"runtime/blender/blender-5.0.1-windows-x64/blender.exe" -b --factory-startup --python-exit-code 1 --python scripts/blender/steps/upscale_orc_basecolor.py -- "data/runtime/orc-rig-orc-refine-r2/orc-neutral.blend" "data/runtime/orc-rig-orc-uv4096/orc-neutral-uv4096.blend" "verification/orc-rig-orc-uv4096/basecolor-0-4096.png" "data/runtime/orc-rig-orc-uv4096/blend-texture-report.json"
```

В браузере base-color подтверждён как 4096 × 4096 в sRGB; roughness и metalness остались 1024. `sceneLint() = []`, `framingReport(...).ok = true`, 25 костей и 9 sockets на месте. Основной вид, четыре ракурса и лицо крупным планом осмотрены: `verification/orc-rig-orc-uv4096/contact-sheet.png`, `face-eyes-tusks.png`. Материал `Орк · Orc neutral rig · UV 4096 · базовый цвет 1` проверен через API и на карточке. GLB и все четыре встроенных изображения завершили загрузку со статусом 200.

## Границы результата

- Проверены техническая загрузка, нейтральная стойка и контрольные позы, а не качество полноценной анимации.
- Перед использованием в production нужны художественная приёмка, локальная правка проблемных весов, проверка самопересечений и отдельная работа над IK/анимационными клипами.
- Edit3D показывает совместимые материалы, скелет и трансформации; полноценная правка рига выполняется в BLEND.
