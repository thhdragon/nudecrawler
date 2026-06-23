from transliterate.base import TranslitLanguagePack, registry


class TgRuLanguagePack(TranslitLanguagePack):
    language_code = "tgru"
    language_name = "tgru"

    character_ranges = ((0x0400, 0x04FF), (0x0500, 0x052F))

    mapping = (
        "abvgdezijklmnoprstufhcC'y'ABVGDEZIJKLMNOPRSTUFH'Y'",
        "абвгдезийклмнопрстуфхцЦъыьАБВГДЕЗИЙКЛМНОПРСТУФХЪЫЬ",
    )

    # reversed_specific_mapping = (
    #    u"ъьЪЬ",
    #    u"''''"
    # )

    pre_processor_mapping = {
        "zh": "ж",
        "yo": "ё",
        "ch": "ч",
        "sh": "ш",
        "sch": "щ",
        "yu": "ю",
        "ya": "я",
        "Yo": "Ё",
        "Zh": "Ж",
        "Ts": "Ц",
        "Ch": "Ч",
        "Sh": "Ш",
        "Sch": "Щ",
        "Yu": "Ю",
        "Ja": "Я",
        "EH": "Э",
        "eh": "э",
    }


registry.register(TgRuLanguagePack)
