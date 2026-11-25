# Third-party notices

Комплект содержит бинарные колёса и исправления сборки для следующих компонентов. Тексты лицензий скопированы без изменений из закреплённых исходников.

| Компонент | Источник и commit | Лицензия / файл |
|---|---|---|
| CuMesh | https://github.com/visualbruno/CuMesh.git — `d10e54c30ddd03d11472c1431693f985501c7966` | MIT — `CuMesh-MIT.txt` |
| FlexGEMM | https://github.com/visualbruno/FlexGEMM.git — `db388bd17c34abc697792aa718dca780734c2783` | MIT — `FlexGEMM-MIT.txt` |
| TRELLIS.2 / O-Voxel | https://github.com/visualbruno/TRELLIS.2.git — `65d1e13b4a92296036044df0633242bb9e95abf6` | MIT — `TRELLIS2-MIT.txt` |
| cubvh (в CuMesh) | вложенный компонент закреплённого CuMesh | MIT — `cubvh-MIT.txt`; NVIDIA Source Code License — `cubvh-NVIDIA-Source-Code-License.txt` |
| xatlas (в CuMesh) | вложенный компонент закреплённого CuMesh | MIT — `xatlas-MIT.txt` |
| Eigen для CuMesh | https://gitlab.com/libeigen/eigen.git — `3147391d946bb4b6c68edd901f2add6ac1f31f8c` | преимущественно MPL-2.0 — `Eigen-CuMesh-MPL-2.0.txt`, пояснение — `Eigen-CuMesh-README.txt` |
| Eigen для O-Voxel | https://gitlab.com/libeigen/eigen.git — `21e4582d1739107337a03460c81412981130373e` | MPL-2.0 — `Eigen-O-Voxel-MPL-2.0.txt` |
| AstraForge scripts | https://github.com/Snodrod/AstraForge | MIT — `AstraForge-MIT.txt` |

AMD-совместимость основывается на работе `tsubasaprogame/AstraForge`, ветка `amd-rocm` (`f9a6b49`), с последующими исправлениями сборки, перечисленными в `README.md`.

## NVIDIA / cubvh — ограничение использования

Файл `cubvh-NVIDIA-Source-Code-License.txt`, раздел 3.3: работа и производные могут использоваться только некоммерчески, для исследования или оценки. Это отдельное условие, которое нельзя игнорировать на основании MIT-лицензий остальных компонентов.

## Eigen / MPL-2.0 — получение исходников

Закреплённые URL и commit указаны выше, а также в `wheels/wheels.json` и `installer/pixal3d/manifest.json`. Скрипт `installer/pixal3d/amd/build-wheels.ps1.txt` после переименования получает эти исходники и воспроизводит сборку. Собственные файлы Eigen в патчах не изменялись.

ROCm 7.2.1 и PyTorch 2.9.1 не включены в данный архив; сборочный скрипт скачивает их из официального `repo.radeon.com` и проверяет SHA-256 по `installer/manifest.json`.
