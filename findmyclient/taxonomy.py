"""NUCC provider taxonomy codes -> labels. Not exhaustive; unknown codes display as the code itself."""

TAXONOMY = {
    "207Q00000X": "Family Medicine",
    "207R00000X": "Internal Medicine",
    "208D00000X": "General Practice",
    "208000000X": "Pediatrics",
    "207P00000X": "Emergency Medicine",
    "207X00000X": "Orthopaedic Surgery",
    "207N00000X": "Dermatology",
    "207V00000X": "Obstetrics & Gynecology",
    "2084P0800X": "Psychiatry",
    "122300000X": "Dentist",
    "1223G0001X": "Dentist, General Practice",
    "225100000X": "Physical Therapist",
    "213E00000X": "Podiatrist",
    "152W00000X": "Optometrist",
    "111N00000X": "Chiropractor",
    "363L00000X": "Nurse Practitioner",
    "261Q00000X": "Clinic/Center",
    "261QP2300X": "Clinic/Center, Primary Care",
    "261QM0801X": "Clinic/Center, Mental Health",
    "261QU0200X": "Clinic/Center, Urgent Care",
    "261QA1903X": "Clinic/Center, Ambulatory Surgical",
    "251E00000X": "Home Health Agency",
}


def label(code: str) -> str:
    return TAXONOMY.get(code, code)
