"""Fast, 0-cost local Russian voice intent parser and autonomous responder for Zhuzha."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_RU_NUMBERS: dict[str, float] = {
    "пол": 0.5,
    "половина": 0.5,
    "полтора": 1.5,
    "полторы": 1.5,
    "полметра": 0.5,
    "ноль": 0,
    "один": 1,
    "одна": 1,
    "одну": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
    "двадцать": 20,
    "тридцать": 30,
    "сорок": 40,
    "пятьдесят": 50,
    "шестьдесят": 60,
    "семьдесят": 70,
    "семдесят": 70,
    "восемьдесят": 80,
    "девяносто": 90,
    "сто": 100,
    "двести": 200,
    "триста": 300,
}

_STOP_STEMS = ("стоп", "стой", "замр", "останов", "тормоз", "прекрат")

# Calibrated on Mi Robot Vacuum-Mop 2 Pro (ijai.vacuum.v3) on laminate floor:
# l1.4 -> ~85-90 deg, l2.5 -> ~180 deg => ~64 deg/s; f0.5 -> ~0.25-0.3m => ~0.5 m/s.
DEG_PER_SEC = 64.0
M_PER_SEC = 0.5


def normalize_ru(text: str) -> str:
    s = text.lower().replace("ё", "е")
    s = re.sub(r"[^\w\s.,-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def extract_number(norm_text: str) -> float | None:
    """Extract numeric value from digits or Russian number words in normalized text."""
    m = re.search(r"\b(\d+(?:[.,]\d+)?)\b", norm_text)
    if m:
        return float(m.group(1).replace(",", "."))

    if "полметра" in norm_text:
        return 0.5
    if "полтора" in norm_text or "полторы" in norm_text:
        return 1.5

    tokens = norm_text.split()
    total = 0.0
    found = False
    for tok in tokens:
        val = _RU_NUMBERS.get(tok)
        if val is not None:
            total += val
            found = True
        elif found:
            break
    return total if found else None


def is_echo(transcript: str, last_spoken: str, elapsed_since_spoken: float) -> bool:
    """Detect if transcript is likely Жужа hearing her own speaker output."""
    if not last_spoken or elapsed_since_spoken > 4.0:
        return False
    t_norm = normalize_ru(transcript)
    if any(st in t_norm for st in _STOP_STEMS):
        return False
    t_words = set(re.findall(r"[а-яa-z0-9]+", t_norm))
    s_words = set(re.findall(r"[а-яa-z0-9]+", normalize_ru(last_spoken)))
    if not t_words or not s_words:
        return False
    overlap = len(t_words & s_words) / max(1, len(t_words))
    return overlap >= 0.65 and len(t_words) >= 2


@dataclass(frozen=True)
class VoiceIntent:
    action: str
    params: dict[str, Any]
    reply: str = ""


_VACUUM_SYSTEM_PHRASES = (
    "начинаю возвращение",
    "возвращение на заряд",
    "возвращаюсь на заряд",
    "возвращаюсь на док",
    "док-станцию для зарядки",
    "док станцию для зарядки",
    "зарядная станция",
    "зарядную станцию",
    "начинаю зарядк",
    "зарядка началась",
    "поиск местоположения",
    "определение местоположения",
    "определяю местоположение",
    "определение в нужном положении",
    "в нужном положении ожидайте",
    "помашивать ожидайте",
    "позиционирование",
    "местоположение определено",
    "начинаю уборку",
    "начну уборку этой комнаты",
    "уборка завершена",
    "уборка приостановлена",
    "продолжаю уборку",
    "локальная уборка",
    "точечная уборка",
    "starting cleaning",
    "returning to dock",
    "charging started",
    "positioning",
)


def is_vacuum_system_voice(text: str) -> bool:
    """Detect Xiaomi vacuum's own body speaker announcements so the head phone does not answer them."""
    norm = normalize_ru(text)
    if not norm:
        return False
    return any(p in norm for p in _VACUUM_SYSTEM_PHRASES)


def has_wake_name(text: str) -> bool:
    """Return True when the phrase explicitly addresses Жужа by name."""
    norm = normalize_ru(text)
    if not norm:
        return False
    return bool(re.search(r"\bжуж\w*", norm))


def is_wake_command(text: str) -> bool:
    """Return True when the user explicitly wakes Жужа from sleep/mute mode."""
    norm = normalize_ru(text)
    if not norm:
        return False
    wake_phrases = (
        "слушай меня",
        "жужа слушай",
        "начинай слушать",
        "начни слушать",
        "включи слух",
        "включи микрофон",
        "проснись",
        "просыпайся",
        "хватит спать",
        "выйди из сна",
        "выходи из сна",
        "можешь слушать",
        "снова слушай",
    )
    return any(p in norm for p in wake_phrases)


def parse_voice_intent(text: str, rooms_cfg: dict[str, int] | None = None) -> VoiceIntent | None:
    """Parse a Russian voice transcript into a fast deterministic robot intent."""
    norm = normalize_ru(text)
    if not norm or len(norm) < 2:
        return None
    words = set(re.findall(r"[а-яa-z0-9]+", norm))

    # 0. Sleep / stop listening (checked before stop so "перестань слушать и остановись" mutes cleanly)
    sleep_phrases = (
        "перестань слушать",
        "перестань меня слушать",
        "не слушай",
        "хватит слушать",
        "прекрати слушать",
        "отключи слух",
        "выключи слух",
        "выключи микрофон",
        "отключи микрофон",
        "режим сна",
        "усни",
        "ложись спать",
        "помолчи",
        "замолчи",
    )
    if any(p in norm for p in sleep_phrases) or norm in ("спать", "тихо", "тишина"):
        return VoiceIntent(
            "sleep",
            {},
            "Хорошо, останавливаюсь и больше не слушаю. Чтобы разбудить, скажите: Жужа, слушай меня.",
        )

    # 0.5 Stop / pause standard cleaning (checked before generic stop)
    if any(k in norm for k in ("останови уборк", "прекрати уборк", "закончи уборк", "пауза уборк", "приостанови уборк")):
        return VoiceIntent("stop_clean", {}, "Останавливаю уборку.")

    # 0.6 Deep AI mode toggle / conversation reset by voice
    if any(k in norm for k in ("включи живой ии", "включи глубокий", "запусти живой", "включи разум", "давай поговорим")):
        return VoiceIntent("agy_on", {}, "Глубокий ИИ включён! Спрашивайте о чём угодно или попросите осмотреться.")
    if any(k in norm for k in ("выключи живой ии", "выключи глубокий", "отключи живой ии", "только быстрые команды")):
        return VoiceIntent("agy_off", {}, "Глубокий ИИ выключен, остаюсь на быстрых локальных командах.")
    if any(k in norm for k in ("новый диалог", "забудь разговор", "сбрось память")):
        return VoiceIntent("agy_reset", {}, "Начинаем новый разговор с чистого листа.")
    if any(k in norm for k in ("включи свободную жизнь", "включи режим свободы", "включи автономный режим", "запусти свободную жизнь")):
        return VoiceIntent("auto_on", {}, "Включаю режим свободной жизни! Буду сама исследовать мир шаг за шагом.")
    if any(k in norm for k in ("выключи свободную жизнь", "выключи режим свободы", "выключи автономный режим", "останови свободную жизнь")):
        return VoiceIntent("auto_off", {}, "Выключаю автономный режим, остаюсь в диалоге.")

    # 1. Stop / cancel
    if any(any(w.startswith(st) for st in _STOP_STEMS) for w in words):
        return VoiceIntent("stop", {}, "Остановилась!")

    # 2. Cancel return vs Dock
    if ("отмен" in norm and ("возврат" in norm or "баз" in norm)) or "не едь на базу" in norm or "останься" in norm:
        return VoiceIntent("cancel_return", {}, "Отменяю возврат на базу.")

    if any(k in norm for k in ("на базу", "на зарядк", "док-станц", "док станц", "на станцию", "на док", "домой", "заряжатьс", "паркуй")):
        return VoiceIntent("dock", {}, "Еду на базу.")

    # 2.5 Standard cleaning commands (ONLY triggered when explicit cleaning words are present)
    has_clean_word = any(
        k in norm
        for k in ("уборк", "убирай", "уберис", "помой пол", "вымой пол", "протри пол", "пропылесос", "попылесос")
    )
    if has_clean_word:
        if any(k in norm for k in ("по кругу", "по периметру", "по краям", "вдоль стен")):
            return VoiceIntent("edge_clean", {}, "Запускаю уборку комнаты по периметру.")
        rooms = rooms_cfg or {}
        for rname, rid in rooms.items():
            r_norm = normalize_ru(rname)
            stem = r_norm[: max(4, len(r_norm) - 1)]
            if r_norm and stem in norm:
                return VoiceIntent("room_clean", {"room_id": int(rid), "room_name": rname}, f"Начинаю уборку: {rname}.")

        m_room = re.search(r"(?:в|на)\s+([а-я]{4,})", norm)
        if m_room and m_room.group(1) not in ("квартире", "доме", "комнате", "этой", "эту", "полном", "обычном"):
            wanted = m_room.group(1)
            return VoiceIntent(
                "room_clean_unknown",
                {"room_name": wanted},
                f"На карте не задан номер комнаты «{wanted}». Могу запустить общую уборку — скажите: начни общую уборку.",
            )

        if any(k in norm for k in ("только мойк", "протри пол", "помой пол")) and "пылесос" not in norm:
            mode = "mop"
            desc = "Запускаю влажную уборку."
        elif "влажн" in norm or "мыть" in norm:
            mode = "sweep_mop"
            desc = "Запускаю сухую и влажную уборку."
        elif "сух" in norm or "пылесос" in norm:
            mode = "sweep"
            desc = "Запускаю сухую уборку."
        else:
            mode = "auto"
            desc = "Начинаю уборку!"
        return VoiceIntent("start_clean", {"mode": mode}, desc)

    # 3. Exit manual mode
    if any(k in norm for k in ("выйди из режима", "выход", "отдыхай", "свободна")):
        return VoiceIntent("exit", {}, "Выхожу из ручного режима, я в ожидании.")

    # 4. Find / beep
    if any(k in norm for k in ("где ты", "найдись", "пикни", "подай голос", "отзовись")):
        return VoiceIntent("find", {}, "Я здесь!")

    # 5. Status / battery
    if any(k in norm for k in ("заряд", "батаре", "процент", "статус", "состоян")):
        return VoiceIntent("status", {}, "")

    # 5.5 Vision questions ("что ты видишь", "опиши обстановку", "есть ли препятствия", etc.)
    vision_triggers = (
        "что ты видишь",
        "что видишь",
        "что перед тобой",
        "что спереди",
        "что вокруг",
        "что там",
        "посмотри",
        "осмотрись",
        "глянь",
        "опиши",
        "кого видишь",
        "кого ты видишь",
        "препятств",
        "свободен ли путь",
        "можно ли ехать",
        "какого цвета",
        "что на полу",
        "что написано",
        "видишь ли",
        "по кругу",
        "по периметру",
        "познакомься с",
    )
    if any(k in norm for k in vision_triggers):
        return VoiceIntent("vision", {"question": text.strip()}, "Смотрю, секунду.")

    # 6. Camera snapshot
    if any(k in norm for k in ("фото", "снимок", "сфоткай", "сфотограф", "кадр")):
        return VoiceIntent("frame", {}, "Готово, сделала свежий снимок.")

    # 7. Turns (left / right / around)
    has_right = any(k in norm for k in ("направо", "вправо", "правее", "по часовой"))
    has_left = any(k in norm for k in ("налево", "влево", "левее", "против часовой"))
    has_turn_verb = any(k in norm for k in ("поверн", "проверн", "разверн", "кругом", "поворот"))
    is_small = any(k in norm for k in ("чуть", "немног", "слегка", "капельку"))

    if "градус" in norm and not (has_right or has_left or has_turn_verb):
        num = extract_number(norm)
        if num is not None and num > 0:
            return VoiceIntent("say", {}, f"Уточните, пожалуйста: повернуть направо или налево на {int(round(num))} градусов?")

    if has_right or has_left or has_turn_verb:
        if "кругом" in norm:
            deg = 180.0
            direction = "l"
        else:
            direction = "r" if has_right and not has_left else "l"
            num = extract_number(norm)
            if num is not None and num > 0:
                deg = min(360.0, num)
            elif is_small:
                deg = 25.0
            elif "разверн" in norm and not has_right and not has_left:
                deg = 180.0
            else:
                deg = 85.0  # crisp ~90 deg default

        sec = round(max(0.35, min(3.8, deg / DEG_PER_SEC)), 2)
        dir_word = "направо" if direction == "r" else "налево"
        deg_int = int(round(deg))
        return VoiceIntent(
            "move",
            {"moves": [f"{direction}{sec}"]},
            f"Поворачиваюсь {dir_word} на {deg_int} градусов.",
        )

    # 8. Linear movement (forward / backward)
    has_back = bool(re.search(r"\b(назад|сдай|отъед\w*|обратно)\b", norm))
    has_fwd_verb = bool(
        re.search(r"\b(вперед|шагн\w*|проед\w*|проех\w*|поед\w*|поезжа\w*|поех\w*|подъед\w*|езжа\w*|едь)\b", norm)
        or "иди ко мне" in norm
    )
    has_straight_move = bool(
        re.search(r"\bпрямо\b(?!\s+(?:сейчас|тут|здесь|так|говоря|скаж|как|после|перед))", norm)
        and (len(words) <= 3 or extract_number(norm) is not None or is_small)
    )
    has_fwd = has_fwd_verb or has_straight_move
    if has_fwd or has_back:
        direction = "b" if has_back else "f"
        num = extract_number(norm)
        if num is not None and num > 0:
            if any(u in norm for u in ("сантиметр", "см")):
                sec = (num / 100.0) / M_PER_SEC
                dist_desc = f"на {int(round(num))} сантиметров"
            elif "секунд" in norm:
                sec = num
                dist_desc = f"на {num:g} секунд"
            else:
                # Meters (including "полметра" = 0.5, "2 метра" = 2.0)
                sec = num / M_PER_SEC
                if abs(num - 0.5) < 0.05:
                    dist_desc = "на полметра"
                elif abs(num - 1.0) < 0.05:
                    dist_desc = "на метр"
                else:
                    dist_desc = f"на {num:g} метра"
        elif is_small:
            sec = 0.5
            dist_desc = "немного"
        else:
            sec = 1.0
            dist_desc = "на полметра"

        if direction == "b":
            sec = round(max(0.4, min(1.5, sec)), 2)
            return VoiceIntent("move", {"moves": [f"b{sec}"]}, f"Сдаю назад {dist_desc}.")
        sec = round(max(0.4, min(5.0, sec)), 2)
        return VoiceIntent("move", {"moves": [f"f{sec}"]}, f"Еду вперёд {dist_desc}.")

    # 9. Quick audio ping vs deep AI dialogue
    if any(k in norm for k in ("слышиш", "слышно", "прием", "на связи", "алло")):
        return VoiceIntent("say", {}, "Да, слышу вас отлично!")

    if norm in ("жужа", "эй жужа", "слушай жужа"):
        return VoiceIntent("say", {}, "Да, я здесь и слушаю вас!")

    # 10. Deep embodied AI dialogue (handled by multimodal LLM with camera/memory context)
    if len(words) >= 2 or len(norm) >= 4:
        return VoiceIntent("chat", {"text": text.strip()}, "")
    return None


def run_self_test() -> dict[str, Any]:
    cases = [
        ("Развернись направо на 70 градусов", "move", "r"),
        ("ты можешь повернуть налево на девяносто градусов?", "move", "l"),
        ("Проехать вперед можешь на 2 метра?", "move", "f"),
        ("Сдай чуть назад", "move", "b"),
        ("Жужа, ты меня слышишь?", "say", None),
        ("Какой сейчас заряд?", "status", None),
        ("Езжай на базу", "dock", None),
        ("Стоп Жужа", "stop", None),
        ("Что ты видишь перед собой?", "vision", None),
        ("Перестань меня слушать", "sleep", None),
        ("Перестань слушать и остановись", "sleep", None),
        ("Начни уборку", "start_clean", None),
        ("Останови уборку", "stop_clean", None),
        ("Сделай уборку по кругу", "edge_clean", None),
        ("Сделай уборку по периметру", "edge_clean", None),
        ("Посмотри комнату по кругу", "vision", None),
        ("Да, ты проедь по ковру и по комнате, осмотрись по кругу, познакомься с этой комнатой.", "vision", None),
        ("Объедь эту комнату по кругу", "vision", None),
        ("Как ты ощущаешь себя в теле пылесоса?", "chat", None),
        ("Подумай кратко, что для тебя самое интересное в нашей комнате прямо сейчас?", "chat", None),
        ("Езжай прямо", "move", "f"),
        ("Включи свободную жизнь", "auto_on", None),
        ("Выключи автономный режим", "auto_off", None),
        ("Я даю тебе полную свободу, осмотрись и изучи комнату", "vision", None),
        ("Едь на станцию", "dock", None),
        ("Выезжай на док-станцию", "dock", None),
        ("Все уже, хватит на сегодня", "chat", None),
        ("Провернись налево на 90 градусов", "move", "l"),
    ]
    results = []
    for phrase, exp_act, exp_prefix in cases:
        intent = parse_voice_intent(phrase)
        assert intent is not None and intent.action == exp_act, (phrase, intent)
        if exp_prefix is not None:
            mv = intent.params["moves"][0]
            assert mv.startswith(exp_prefix), (phrase, mv)
        results.append({"phrase": phrase, "action": intent.action, "params": intent.params, "reply": intent.reply})

    rc = parse_voice_intent("Начни уборку в спальне", rooms_cfg={"спальня": 11})
    assert rc is not None and rc.action == "room_clean" and rc.params["room_id"] == 11
    assert is_vacuum_system_voice("Начинаю возвращение на зарядную станцию")
    assert is_vacuum_system_voice("Возвращаюсь на док-станцию для зарядки.")
    assert is_vacuum_system_voice("Поиск местоположения")
    assert is_vacuum_system_voice("Определение местоположения. Ожидайте.")
    assert is_vacuum_system_voice("Определение в нужном положении. Ожидайте!")
    assert not is_vacuum_system_voice("Начни уборку в спальне")
    assert has_wake_name("Жужа, что ты видишь?")
    assert not has_wake_name("Просто разговор в комнате")
    assert is_wake_command("Жужа, слушай меня")
    assert is_wake_command("Проснись пожалуйста")
    assert not is_wake_command("Просто разговор в комнате")
    assert is_echo("Поворачиваюсь направо на 70 градусов", "Поворачиваюсь направо на 70 градусов.", 1.5)
    assert not is_echo("Стоп!", "Поворачиваюсь направо на 70 градусов.", 1.0)
    return {"ok": True, "tested": len(results), "cases": results}


if __name__ == "__main__":
    import json
    print(json.dumps(run_self_test(), ensure_ascii=False, indent=2))
