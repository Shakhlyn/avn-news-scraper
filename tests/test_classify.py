import pytest

POSITIVE = [
    # (category, title, body)
    ("extrajudicial_killing", "Two killed in 'gunfight' with RAB in Cox's Bazar", "Rights groups call it extrajudicial killing."),
    ("extrajudicial_killing", "কক্সবাজারে র‍্যাবের সঙ্গে বন্দুকযুদ্ধে দুজন নিহত", ""),
    ("extrajudicial_killing", "", "পরিবারের দাবি, তাকে ক্রসফায়ারে হত্যা করা হয়েছে।"),
    ("border_killing", "Youth shot dead by BSF at Lalmonirhat border", ""),
    ("border_killing", "লালমনিরহাট সীমান্তে বিএসএফের গুলিতে যুবক নিহত", ""),
    ("enforced_disappearance", "Commission receives 1,600 complaints of enforced disappearance", ""),
    ("enforced_disappearance", "আয়নাঘরে আট বছর: ফিরে আসার গল্প", ""),
    ("enforced_disappearance", "", "২০১৩ সালে তাকে গুম করা হয় বলে পরিবারের অভিযোগ।"),
    ("custodial_torture_death", "Man dies in police custody in Gazipur", ""),
    ("custodial_torture_death", "থানা হেফাজতে যুবকের মৃত্যু, পরিবারের অভিযোগ নির্যাতন", ""),
    ("custodial_torture_death", "", "রিমান্ডে নির্যাতনের অভিযোগ করেছেন আসামির আইনজীবী।"),
    ("kneecapping", "Police shot him in the leg during arrest, family alleges", ""),
    ("kneecapping", "আটকের পর পায়ে গুলি করার অভিযোগ", ""),
    ("death_penalty", "Five sentenced to death for murder", ""),
    ("death_penalty", "হত্যা মামলায় তিনজনের মৃত্যুদণ্ড", ""),
    ("death_penalty", "হত্যা মামলায় তিনজনের মৃত্যুদন্ড", ""),
    ("digital_security_act", "Journalist arrested under Digital Security Act", ""),
    ("digital_security_act", "সাংবাদিকের বিরুদ্ধে ডিজিটাল নিরাপত্তা আইনে মামলা", ""),
    ("digital_security_act", "", "আইসিটি আইনের ৫৭ ধারায় করা মামলাটি খারিজ হয়েছে।"),
    ("july_uprising", "July uprising: families of martyrs still await justice", ""),
    ("july_uprising", "জুলাই গণঅভ্যুত্থানে শহীদ পরিবারের পাশে", ""),
    ("july_uprising", "৩৬ জুলাই স্মরণে কর্মসূচি", ""),
]

NEGATIVE = [
    ("extrajudicial_killing", "Bangladesh beat India in a thrilling encounter at Mirpur", ""),
    ("enforced_disappearance", "আবহাওয়া গুমোট, দিনভর বৃষ্টির সম্ভাবনা", ""),
    ("death_penalty", "Government executed the new budget plan smoothly", ""),
    ("kneecapping", "Footballer misses match with leg injury", ""),
    ("border_killing", "BGB and BSF hold flag meeting on trade", ""),
    ("july_uprising", "Mango prices rise in July", ""),
]


@pytest.mark.parametrize("category,title,body", POSITIVE)
def test_positive(classifier, category, title, body):
    found = {m.category for m in classifier.classify(title, body)}
    assert category in found, f"{category} not found for {title or body!r}; got {found}"


@pytest.mark.parametrize("category,title,body", NEGATIVE)
def test_negative(classifier, category, title, body):
    found = {m.category for m in classifier.classify(title, body)}
    assert category not in found


def test_matched_terms_recorded(classifier):
    [m] = [m for m in classifier.classify("Youth shot dead by BSF at border", "") if m.category == "border_killing"]
    assert m.matched_terms and m.score >= classifier.min_score


def test_prefilter(classifier):
    assert classifier.prefilter("সীমান্তে বিএসএফের গুলিতে যুবক নিহত")
    assert classifier.prefilter("", "", "news/bangladesh/crime/crossfire-in-teknaf-2")
    assert not classifier.prefilter("Stock market gains for third day")


@pytest.mark.parametrize("category,title,body", [
    ("enforced_disappearance", "নিখোঁজের দুই দিন পর শিশুর মরদেহ উদ্ধার", "শিশুটি নিখোঁজ ছিল। নিখোঁজ শিশুর লাশ পাওয়া গেছে।"),
    ("enforced_disappearance", "পিটিয়ে হত্যা করে লাশ গুম করে রাখে বন্ধুরা", ""),
    ("custodial_torture_death", "সাবেক মেয়রসহ ১৪ জন রিমান্ডে", "আদালত তাদের রিমান্ডে পাঠিয়েছে।"),
    ("july_uprising", "জুলাই সনদ বাস্তবায়নের দাবি", "গণঅভ্যুত্থানের পর জুলাই সনদ স্বাক্ষর হয়।"),
])
def test_weak_terms_alone_do_not_match(classifier, category, title, body):
    found = {m.category for m in classifier.classify(title, body)}
    assert category not in found
