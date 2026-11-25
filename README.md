# Оверлей Luciole

[English](README.en.md)

Оверлей содержит `gui-apps/luciole` для `~amd64`. Luciole — файловый менеджер
для Wayland.

## Подготовка снимков

Версия снимка имеет вид `<VERSION>_pre<YYYYMMDDHHMMSS>`. В начале стоит
версия `X.Y.Z` из файла `VERSION` проекта Luciole, в конце — время коммита
в UTC из поля committer. Принятые снимки определяются по именам ebuild и
значениям `MY_COMMIT` в них. Текущий снимок — самый новый. При подготовке
обновления он становится предыдущим, а самый старый ebuild исключается из
подготовленной копии, если в оверлее уже есть два снимка.

Запускайте команды из корня оверлея. Нужны Python 3.10 или новее и доступ
к публичному API GitHub.

```sh
python3 scripts/prepare_luciole.py --dry-run
python3 scripts/prepare_luciole.py --stage-dir /tmp
```

При каждом запуске скрипт один раз получает SHA ветки `main` проекта Luciole
и использует этот коммит для проверки. В режиме `--dry-run` скрипт показывает
версию, SHA, список сохраняемых и удаляемых снимков и изменения в файлах
сборки и лицензий. Файлы при этом не создаются. Если этот SHA уже указан в
текущем ebuild, обе команды завершаются без подготовки кандидата.

Скрипт создаёт новый каталог в указанной папке за пределами рабочей копии
оверлея. Он копирует файлы пакета и добавляет ebuild, в котором меняет только
значение `MY_COMMIT`. Изменения в файлах проекта Luciole выводятся для
проверки владельцем оверлея и сохраняются в `candidate.json`. В каталоге
остаётся копия принятого Manifest и метка `INCOMPLETE.txt`.

## Проверка подготовленного снимка

Потребуются Docker с поддержкой Linux amd64, Git и доступ к сети для
загрузки образов, архивов и инструментов проверки. Проверка использует
закреплённую пару образов с деревом Gentoo и stage3.

```sh
docker pull --platform linux/amd64 gentoo/portage@sha256:ce69e5b771bfe9404731e846b835346c34f2c82817ac3626f13af198689a1961
docker pull --platform linux/amd64 gentoo/stage3@sha256:d8536e52677e3efe4caf048ca588bc685bc2d24d7fdfc882de1cdc6d38c81d96
python3 scripts/prepare_luciole.py --stage-dir /tmp
stage=/tmp/luciole-candidate-REPLACE_ME
python3 -B scripts/validate_luciole.py validate "$stage"
python3 -B scripts/validate_luciole.py verify "$stage/handoff"
```

Подставьте в `stage` путь, который вывела команда подготовки. Скрипт
печатает точную команду Docker. Дерево Gentoo и копия подготовленного
пакета подключаются только для чтения. Оверлей настраивается во временном
контейнере с отключённой синхронизацией репозиториев.

Скрипт сверяет набор ebuild и их SHA с исходным коммитом оверлея,
проверяет порядок версий через `vercmp` из Portage, загружает сохранённый
архив и сверяет его размер и хеши с принятым Manifest. Затем он запускает
`ebuild <candidate-ebuild> manifest`. Новый Manifest должен содержать ровно
две записи DIST с именами архивов из подготовленных ebuild. Запись
сохранённого архива остаётся прежней. Скрипт проверяет оба архива,
запускает `pkgcheck scan gui-apps/luciole --exit` и проверяет зависимости
точного атома кандидата через `emerge -pv --with-bdeps=y --autounmask=n`.
Настройки KEYWORDS берутся из stage3. При необходимости `~amd64`
разрешается для точного атома кандидата. USE-флаг `test` отключается
для этого же атома.

В закреплённом профиле `ARCH=amd64` и `ACCEPT_KEYWORDS=amd64`.
Для требуемой Luciole версии `wayland-protocols-1.49` нужно разрешить
`~amd64`. Portage также потребовал `gdk-pixbuf[jpeg]` для Luciole,
`cairo[X]` и `libglvnd[X]` для GTK, `freetype[harfbuzz]` для Pango, `gcr:0[gtk]`
по цепочке зависимостей GVfs и `mesa[wayland]` для EGL/OpenGL ES. Все
записи относятся к точным версиям и действуют только в контейнере.

Для отдельной проверки зависимостей с Wayland и включённым `test`
использовалась та же команда `emerge -pv --with-bdeps=y --autounmask=n`.
В контейнере для точного атома Luciole указали `test` вместо `-test`,
а в `package.use` добавили `=media-libs/libheif-1.21.2 gdk-pixbuf` и
`=media-video/ffmpeg-8.1.2 libaom webp x264`. В артефакте сохраняется
результат обязательной проверки с выключенным `test`.

После успешных проверок в `handoff/` появляются `handoff.json`, новый
Manifest и два ebuild в каталоге `overlay/gui-apps/luciole/`. JSON содержит
исходный коммит оверлея, версии и SHA Luciole, ссылку на сравнение,
результаты проверок, контрольные суммы и список ожидаемых добавлений,
изменений и удалений. Скрипт сверяет эти файлы с исходным коммитом перед
снятием метки незавершённой подготовки. Если проверка завершается ошибкой,
метка сохраняется.

Подготовка создаёт копию файлов пакета. Проверка подтверждает корректность
метаданных и архивов и разрешимость зависимостей. Сборка Luciole, запуск
тестов проекта, проверка в сеансе Wayland и публикация выполняются отдельно.

## Сборка и требования к сеансу

Для сборки нужны компилятор C++23 и стандартная библиотека с поддержкой
`std::print`, `std::expected` и `std::move_only_function`. Разработчики
Luciole проверили GCC/libstdc++ 16.2. Минимальную версию компилятора ещё
предстоит проверить сборкой.

Для работы нужны драйвер EGL/OpenGL ES с поддержкой Wayland и сеансовая шина
D-Bus. Для корзины требуется GVfs. Для использования Luciole в качестве диалога
выбора файлов нужны `sys-apps/xdg-desktop-portal` и выбор бэкенда `luciole`
в настройках сеанса.

В Gentoo GdkPixbuf использует собственные загрузчики изображений, а поддержка
Glycin отключена. USE-флаг `test` включает тесты Meson. По документации
Luciole, одному из тестов нужен Glycin для чтения ICO со встроенным PNG.

## Дополнительные функции

| Функция | Пакет или настройка Gentoo |
| --- | --- |
| SFTP, SMB, WebDAV | Для нужного протокола включите `sftp`, `samba` или `http` у `gnome-base/gvfs`. Для защищённого WebDAV нужен `net-libs/glib-networking[ssl]`. |
| Обнаружение узлов через DNS-SD | `gnome-base/gvfs[zeroconf]` и работающая служба Avahi |
| Монтирование устройств и разблокировка LUKS | `gnome-base/gvfs[udisks]` и работающая служба UDisks2 |
| Эскизы и метаданные медиафайлов | `media-video/ffmpeg`, для ленты кадров видео — `media-video/ffmpegthumbnailer[png]` |
| Эскизы и метаданные PDF | `app-text/poppler[utils]` |
| Зашифрованные архивы 7z | `app-arch/7zip` |
| Изображения WebP, AVIF/HEIF и JPEG XL | Соответственно `gui-libs/gdk-pixbuf-loader-webp`, `media-libs/libheif[gdk-pixbuf]` и `media-libs/libjxl[gdk-pixbuf]` |

[Документация Luciole по сборке пакетов в ветке main](https://github.com/noctalia-dev/luciole/blob/main/PACKAGING.md)
