"""Best-effort detection of the Bangladeshi district(s) an article is about."""

from __future__ import annotations

from collections import Counter

from .classify import compile_term
from .normalize import normalize

# district -> spellings (English variants + Bangla)
DISTRICTS: dict[str, list[str]] = {
    # Dhaka division
    "Dhaka": ["Dhaka", "ঢাকা"],
    "Faridpur": ["Faridpur", "ফরিদপুর"],
    "Gazipur": ["Gazipur", "গাজীপুর"],
    "Gopalganj": ["Gopalganj", "গোপালগঞ্জ"],
    "Kishoreganj": ["Kishoreganj", "কিশোরগঞ্জ"],
    "Madaripur": ["Madaripur", "মাদারীপুর"],
    "Manikganj": ["Manikganj", "মানিকগঞ্জ"],
    "Munshiganj": ["Munshiganj", "মুন্সীগঞ্জ", "মুন্সিগঞ্জ"],
    "Narayanganj": ["Narayanganj", "নারায়ণগঞ্জ"],
    "Narsingdi": ["Narsingdi", "নরসিংদী"],
    "Rajbari": ["Rajbari", "রাজবাড়ী"],
    "Shariatpur": ["Shariatpur", "শরীয়তপুর"],
    "Tangail": ["Tangail", "টাঙ্গাইল", "টাঙাইল"],
    # Chattogram division
    "Bandarban": ["Bandarban", "বান্দরবান"],
    "Brahmanbaria": ["Brahmanbaria", "ব্রাহ্মণবাড়িয়া"],
    "Chandpur": ["Chandpur", "চাঁদপুর"],
    "Chattogram": ["Chattogram", "Chittagong", "চট্টগ্রাম"],
    "Cumilla": ["Cumilla", "Comilla", "কুমিল্লা"],
    "Cox's Bazar": ["Cox's Bazar", "Coxs Bazar", "Cox's Bazaar", "কক্সবাজার"],
    "Feni": ["Feni", "ফেনী"],
    "Khagrachhari": ["Khagrachhari", "Khagrachari", "খাগড়াছড়ি"],
    "Lakshmipur": ["Lakshmipur", "Laxmipur", "লক্ষ্মীপুর"],
    "Noakhali": ["Noakhali", "নোয়াখালী"],
    "Rangamati": ["Rangamati", "রাঙামাটি", "রাঙ্গামাটি"],
    # Rajshahi division
    "Bogura": ["Bogura", "Bogra", "বগুড়া"],
    "Joypurhat": ["Joypurhat", "জয়পুরহাট"],
    "Naogaon": ["Naogaon", "নওগাঁ"],
    "Natore": ["Natore", "নাটোর"],
    "Chapainawabganj": ["Chapainawabganj", "Chapai Nawabganj", "চাঁপাইনবাবগঞ্জ"],
    "Pabna": ["Pabna", "পাবনা"],
    "Rajshahi": ["Rajshahi", "রাজশাহী"],
    "Sirajganj": ["Sirajganj", "সিরাজগঞ্জ"],
    # Khulna division
    "Bagerhat": ["Bagerhat", "বাগেরহাট"],
    "Chuadanga": ["Chuadanga", "চুয়াডাঙ্গা", "চুয়াডাঙা"],
    "Jashore": ["Jashore", "Jessore", "যশোর"],
    "Jhenaidah": ["Jhenaidah", "ঝিনাইদহ"],
    "Khulna": ["Khulna", "খুলনা"],
    "Kushtia": ["Kushtia", "কুষ্টিয়া"],
    "Magura": ["Magura", "মাগুরা"],
    "Meherpur": ["Meherpur", "মেহেরপুর"],
    "Narail": ["Narail", "নড়াইল"],
    "Satkhira": ["Satkhira", "সাতক্ষীরা"],
    # Barishal division
    "Barguna": ["Barguna", "বরগুনা"],
    "Barishal": ["Barishal", "Barisal", "বরিশাল"],
    "Bhola": ["Bhola", "ভোলা"],
    "Jhalokathi": ["Jhalokathi", "Jhalokati", "ঝালকাঠি"],
    "Patuakhali": ["Patuakhali", "পটুয়াখালী"],
    "Pirojpur": ["Pirojpur", "পিরোজপুর"],
    # Sylhet division
    "Habiganj": ["Habiganj", "হবিগঞ্জ"],
    "Moulvibazar": ["Moulvibazar", "Maulvibazar", "মৌলভীবাজার"],
    "Sunamganj": ["Sunamganj", "সুনামগঞ্জ"],
    "Sylhet": ["Sylhet", "সিলেট"],
    # Rangpur division
    "Dinajpur": ["Dinajpur", "দিনাজপুর"],
    "Gaibandha": ["Gaibandha", "গাইবান্ধা"],
    "Kurigram": ["Kurigram", "কুড়িগ্রাম"],
    "Lalmonirhat": ["Lalmonirhat", "লালমনিরহাট"],
    "Nilphamari": ["Nilphamari", "নীলফামারী"],
    "Panchagarh": ["Panchagarh", "পঞ্চগড়"],
    "Rangpur": ["Rangpur", "রংপুর"],
    "Thakurgaon": ["Thakurgaon", "ঠাকুরগাঁও"],
    # Mymensingh division
    "Jamalpur": ["Jamalpur", "জামালপুর"],
    "Mymensingh": ["Mymensingh", "ময়মনসিংহ"],
    "Netrokona": ["Netrokona", "Netrakona", "নেত্রকোনা"],
    "Sherpur": ["Sherpur", "শেরপুর"],
}

_PATTERNS = {name: [compile_term(s).pattern for s in spellings] for name, spellings in DISTRICTS.items()}


def detect_districts(title: str, body: str, *, body_chars: int = 2500) -> tuple[str | None, list[str]]:
    """Return (most likely district, all districts mentioned).

    Headline mentions count triple; only the start of the body is used because
    reports usually name the place in the dateline / first paragraph.
    """
    t = normalize(title)
    b = normalize(body[:body_chars])
    scores: Counter[str] = Counter()
    for name, patterns in _PATTERNS.items():
        for p in patterns:
            scores[name] += 3 * len(p.findall(t)) + len(p.findall(b))
    mentioned = [name for name, n in scores.most_common() if n > 0]
    return (mentioned[0] if mentioned else None), mentioned
