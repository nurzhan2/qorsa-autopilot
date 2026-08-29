"""Excel-чеклист для клиента: что нужно от него, чтобы довести проект до конца.

Источник — только brief и access_items проекта 8 в БД, ничего не выдумываем.
Одноразовый скрипт, не часть приложения — запускается руками, когда нужен
свежий файл для клиента.
"""
from __future__ import annotations

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

OUT = "outputs/client_requirements_tur_tuk.xlsx"

# (что нужно, категория, зачем, формат/пример, блокирует)
# Порядок — как в файле: блокирующие решения первыми, дальше по категориям.
ROWS = [
    ("Как считать стоимость доставки клиенту — фиксированная ставка, "
     "по зонам или по километрам?",
     "решение",
     "без этого нельзя посчитать сумму заказа с доставкой",
     "короткий ответ; если по зонам — список зон с ценой",
     "да"),
    ("Какую платёжную систему подключить для приёма карт и СБП?",
     "решение",
     "без выбранного провайдера нельзя запустить оплату онлайн",
     "название сервиса (например, ЮKassa, Т-Касса) — или просьба помочь выбрать",
     "да"),

    ("Доступ к личному кабинету платёжной системы",
     "доступ",
     "чтобы подключить приём платежей в приложении — после выбора провайдера (см. выше)",
     "логин/доступ в личный кабинет провайдера или отдельная учётка для разработчика",
     "нет"),
    ("Доступ к картографическому сервису",
     "доступ",
     "показывает курьеру маршрут и клиенту — где находится заказ",
     "аккаунт в сервисе карт (например, Яндекс.Карты, 2ГИС) с доступом для разработчика",
     "нет"),

    ("Логотип и фирменный стиль",
     "материал",
     "оформление приложения в цветах бренда",
     "файлы логотипа (PNG/SVG) в белом и бордовом — у вас уже готово, пришлите файлы",
     "нет"),
    ("Список отелей и гостиниц Кемера для зон доставки",
     "материал",
     "приложение должно знать, куда может доставлять",
     "список названий отелей — таблицей или текстом",
     "нет"),
    ("Карточки товаров и описания",
     "материал",
     "наполнение каталога",
     "на каждый товар: название, фото, описание, цена",
     "нет"),

    ("Аккаунт Apple Developer Program",
     "регистрация",
     "без него нельзя опубликовать приложение в App Store",
     "регистрация на developer.apple.com, платная, на ваше юр- или физлицо",
     "нет"),
    ("Аккаунт Google Play Console",
     "регистрация",
     "без него нельзя опубликовать приложение в Google Play",
     "регистрация на play.google.com/console, разовый платёж",
     "нет"),
    ("Эквайринг — договор с платёжным провайдером",
     "регистрация",
     "чтобы юридически принимать оплату картами и СБП на счёт компании",
     "договор на ваше юрлицо с выбранным провайдером — после решения по платёжной системе",
     "нет"),
]

HEADERS = ["№", "Что нужно", "Категория", "Зачем это в проекте",
          "Формат или пример", "Блокирует ли работу сейчас", "Статус"]

INSTRUCTION = ('Отметьте в последней колонке, что готово, или пришлите. '
              'Пункты с "да" в колонке блокировки нужны в первую очередь.')

FONT_NAME = "Arial"
HEADER_FILL = PatternFill("solid", fgColor="D9D9D9")
BLOCK_FILL = PatternFill("solid", fgColor="FCE4E4")


def build() -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Что нужно от вас"

    # строка-инструкция
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(HEADERS))
    cell = ws.cell(row=1, column=1, value=INSTRUCTION)
    cell.font = Font(name=FONT_NAME, italic=True)
    cell.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[1].height = 30

    # шапка
    for col, title in enumerate(HEADERS, start=1):
        c = ws.cell(row=2, column=col, value=title)
        c.font = Font(name=FONT_NAME, bold=True)
        c.fill = HEADER_FILL
        c.alignment = Alignment(wrap_text=True, vertical="center")

    # строки данных
    r = 3
    for n, (what, category, why, fmt, blocking) in enumerate(ROWS, start=1):
        values = [n, what, category, why, fmt, blocking, ""]
        for col, val in enumerate(values, start=1):
            c = ws.cell(row=r, column=col, value=val)
            c.font = Font(name=FONT_NAME)
            c.alignment = Alignment(wrap_text=True, vertical="top")
            if blocking == "да":
                c.fill = BLOCK_FILL
        r += 1

    ws.freeze_panes = "A3"

    # ширины по содержимому (грубо: по самой длинной строке в столбце, с потолком)
    caps = {1: 6, 2: 55, 3: 14, 4: 45, 5: 55, 6: 14, 7: 14}
    for col in range(1, len(HEADERS) + 1):
        longest = 0
        for row in ws.iter_rows(min_col=col, max_col=col, min_row=1, max_row=r - 1):
            for c in row:
                if c.value:
                    longest = max(longest, len(str(c.value)))
        ws.column_dimensions[get_column_letter(col)].width = min(caps[col], max(10, longest * 0.9))

    wb.save(OUT)
    print(f"сохранено: {OUT}")
    print(f"строк данных: {len(ROWS)}")


if __name__ == "__main__":
    build()
