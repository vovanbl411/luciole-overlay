# Оверлей Luciole

[English](README.en.md)

Оверлей содержит `gui-apps/luciole-0.1.0_pre20261124203731` для `~amd64`.
Luciole — файловый менеджер для Wayland. Версия привязана к коммиту
`5505f60d28a3919d4e0ec25d61db040e82151f0c`.

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

[Документация Luciole по сборке пакетов](https://github.com/noctalia-dev/luciole/blob/5505f60d28a3919d4e0ec25d61db040e82151f0c/PACKAGING.md)
