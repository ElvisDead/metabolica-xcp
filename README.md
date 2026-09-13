# Metabolica Living XCP — GitHub Pages + Actions

Версия без собственного сервера и без домена.

## Как работает

- `assets.json` содержит список XCP-активов.
- GitHub Actions раз в 5 минут читает Counterparty history.
- `generate_site.py` создаёт в `docs/`:
  - `0001.json`
  - `0001.png` (48×48)
  - `0001-card.png` (560×400)
  - `0001.svg`
  - `0001/state.json`
  - `0001/index.html`
- Тот же GitHub Action сразу публикует папку `docs/` в GitHub Pages.

Если очередное обновление не сработает, предыдущая уже опубликованная версия сайта остаётся доступной.

## Установка через веб-интерфейс GitHub

1. Создай публичный репозиторий, например `metabolica-xcp`.
2. Загрузи все файлы и папки из ZIP в корень репозитория, включая `.github/workflows/update-pages.yml`.
3. Открой **Settings → Pages**.
4. В **Build and deployment → Source** выбери **GitHub Actions**.
5. Открой вкладку **Actions**.
6. Выбери **Update and deploy Metabolica Pages**.
7. Нажми **Run workflow → Run workflow**.
8. После зелёного завершения открой **Settings → Pages**: GitHub покажет публичный URL сайта.

Ожидаемые адреса:

- `https://USERNAME.github.io/REPO/`
- `https://USERNAME.github.io/REPO/0001.json`
- `https://USERNAME.github.io/REPO/0001.png`
- `https://USERNAME.github.io/REPO/0001-card.png`
- `https://USERNAME.github.io/REPO/0001/`

Для Counterparty description `METABOLICA.0001` используйте URL `.../0001.json` только после того, как он реально открывается в браузере.

## Конфигурация первого актива

`assets.json` уже содержит:

```json
[
  {
    "asset": "METABOLICA.0001",
    "slug": "0001",
    "title": "Metabolica Living XCP #0001"
  }
]
```

## Важное ограничение

Scheduled Actions у публичного репозитория GitHub может отключить после длительного отсутствия активности. Поэтому для долгоживущего проекта стоит периодически контролировать вкладку Actions. Канонический источник изображения всё равно остаётся Counterparty history + открытый renderer.
