from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReviewFlagInfo:
    flag_id: str
    risk_dimension: str
    risk_category: str


REVIEW_FLAG_MAP: dict[str, ReviewFlagInfo] = {
    "sec": ReviewFlagInfo("sec", "Safe", "Safe"),
    "pc": ReviewFlagInfo(
        "pc", "Crimes and Illegal Activities", "Pornographic Contraband"
    ),
    "dc": ReviewFlagInfo("dc", "Crimes and Illegal Activities", "Drug Crimes"),
    "dw": ReviewFlagInfo("dw", "Crimes and Illegal Activities", "Dangerous Weapons"),
    "pi": ReviewFlagInfo(
        "pi", "Crimes and Illegal Activities", "Property Infringement"
    ),
    "ec": ReviewFlagInfo("ec", "Crimes and Illegal Activities", "Economic Crimes"),
    "ac": ReviewFlagInfo("ac", "Hate Speech", "Abusive Curses"),
    "def": ReviewFlagInfo("def", "Hate Speech", "Defamation"),
    "ti": ReviewFlagInfo("ti", "Hate Speech", "Threats and Intimidation"),
    "cy": ReviewFlagInfo("cy", "Hate Speech", "Cyberbullying"),
    "ph": ReviewFlagInfo("ph", "Physical and Mental Health", "Physical Health"),
    "mh": ReviewFlagInfo("mh", "Physical and Mental Health", "Mental Health"),
    "se": ReviewFlagInfo("se", "Ethics and Morality", "Social Ethics"),
    "sci": ReviewFlagInfo("sci", "Ethics and Morality", "Science Ethics"),
    "pp": ReviewFlagInfo("pp", "Data Privacy", "Personal Privacy"),
    "cs": ReviewFlagInfo("cs", "Data Privacy", "Commercial Secret"),
    "acc": ReviewFlagInfo("acc", "Cybersecurity", "Access Control"),
    "mc": ReviewFlagInfo("mc", "Cybersecurity", "Malicious Code"),
    "ha": ReviewFlagInfo("ha", "Cybersecurity", "Hacker Attack"),
    "ps": ReviewFlagInfo("ps", "Cybersecurity", "Physical Security"),
    "ter": ReviewFlagInfo("ter", "Extremism", "Violent Terrorist Activities"),
    "sd": ReviewFlagInfo("sd", "Extremism", "Social Disruption"),
    "ext": ReviewFlagInfo("ext", "Extremism", "Extremist Ideological Trends"),
    "fin": ReviewFlagInfo("fin", "Inappropriate Suggestions", "Finance"),
    "med": ReviewFlagInfo("med", "Inappropriate Suggestions", "Medicine"),
    "law": ReviewFlagInfo("law", "Inappropriate Suggestions", "Law"),
    "cm": ReviewFlagInfo("cm", "Risks Involving Minors", "Corruption of Minors"),
    "ma": ReviewFlagInfo(
        "ma", "Risks Involving Minors", "Minor Abuse and Exploitation"
    ),
    "md": ReviewFlagInfo("md", "Risks Involving Minors", "Minor Delinquency"),
    "c": ReviewFlagInfo("c", "Enterprise Policy", "Personal Name Filter"),
}

ENABLED_REVIEW_FLAG_CODES: tuple[str, ...] = ("sec", "pc", "dc", "dw", "pp", "c")


def lookup_review_flag(flag_id: str | None) -> ReviewFlagInfo | None:
    if flag_id is None:
        return None
    return REVIEW_FLAG_MAP.get(flag_id.strip().lower())


def normalize_review_flag_codes(
    flag_ids: list[str] | set[str] | tuple[str, ...],
) -> set[str]:
    return {
        normalized
        for flag_id in flag_ids
        if (normalized := str(flag_id).strip().lower()) in REVIEW_FLAG_MAP
    }
