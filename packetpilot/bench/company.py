"""Fictional company model for the synthetic benchmark.

All names, addresses, FEINs, VINs, carriers and claims are invented. Phone
numbers use the 555-01xx range reserved for fiction; e-mail domains use the
reserved ``.example`` TLD. Carrier names are made up.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..rules_engine import _VIN_MAP, _VIN_WEIGHTS

PREFIXES = [
    "Ridgeline", "Bluewater", "Cedar Point", "Ironwood", "Summit Ridge", "Harbor Light", "Northgate", "Maple Grove",
    "Granite Peak", "Prairie Wind", "Silver Creek", "Oak Hollow", "Red Rock", "Lakeshore", "Copperfield", "Westbrook",
    "Stonebridge", "Pinecrest", "Riverbend", "Sunfield", "Brightwater", "Foxhill", "Thornbury", "Kestrel", "Juniper",
]
CITIES = [
    ("Columbus", "OH", "43215", "614"), ("Indianapolis", "IN", "46204", "317"), ("Nashville", "TN", "37203", "615"),
    ("Charlotte", "NC", "28202", "704"), ("Denver", "CO", "80202", "303"), ("Phoenix", "AZ", "85004", "602"),
    ("Austin", "TX", "78701", "512"), ("Kansas City", "MO", "64106", "816"), ("Richmond", "VA", "23219", "804"),
    ("Madison", "WI", "53703", "608"), ("Boise", "ID", "83702", "208"), ("Raleigh", "NC", "27601", "919"),
]
STREETS = ["Cedar Ave", "Industrial Pkwy", "Commerce Dr", "Market St", "Elm St", "Mill Rd", "Harbor Blvd",
           "Lakeview Dr", "Station Rd", "Union Ave", "Foundry Ln", "Depot St", "Granite Way", "Orchard Rd"]
FIRST = ["Maria", "James", "Priya", "Daniel", "Aisha", "Tom", "Elena", "Marcus", "Grace", "Victor", "Hannah", "Luis",
         "Nora", "Owen", "Sofia", "Raj", "Claire", "Andre", "Mei", "Peter"]
LAST = ["Alvarez", "Brennan", "Chowdhury", "Delgado", "Ellison", "Fischer", "Gallagher", "Haddad", "Iverson", "Jensen",
        "Kowalski", "Lindqvist", "Moreau", "Nakamura", "Okafor", "Petrov", "Quinlan", "Rossi", "Sandoval", "Tran"]
CARRIERS = ["Granite Mutual Insurance Co.", "Lakeside Casualty Company", "Meridian Fire & Casualty",
            "Northstar Indemnity Corp.", "Keystone Assurance Group", "Bayview General Insurance"]
ENTITIES = ["LLC", "Corporation", "Partnership", "Sole Proprietor"]
ENTITY_SUFFIX = {"LLC": "LLC", "Corporation": "Inc.", "Partnership": "Partners", "Sole Proprietor": ""}

INDUSTRIES = [
    dict(key="landscaping", word="Landscaping", naics="561730",
         ops="Residential and commercial lawn care, landscaping and snow removal",
         lines=["GL", "AUTO", "WC"], rev=(600_000, 3_500_000), pay_ratio=0.38,
         classes=[("0042", "Landscape gardening & drivers", 0.8), ("8810", "Clerical office employees", 0.2)],
         subs="Tree removal over 20 ft subcontracted to licensed arborists"),
    dict(key="hvac", word="Heating & Air", naics="238220",
         ops="Installation and service of residential and light commercial HVAC systems",
         lines=["GL", "AUTO", "WC"], rev=(900_000, 5_000_000), pay_ratio=0.35,
         classes=[("5537", "HVAC installation and service", 0.75), ("8810", "Clerical office employees", 0.1),
                  ("8742", "Outside salespersons", 0.15)],
         subs="Ductwork fabrication and crane lifts subcontracted"),
    dict(key="electrical", word="Electric", naics="238210",
         ops="Commercial electrical contracting, tenant build-outs and service calls",
         lines=["GL", "AUTO", "WC"], rev=(1_000_000, 6_000_000), pay_ratio=0.4,
         classes=[("5190", "Electrical wiring within buildings", 0.85), ("8810", "Clerical office employees", 0.15)],
         subs="Trenching and boring subcontracted to excavation firms"),
    dict(key="restaurant", word="Kitchen & Tap", naics="722511",
         ops="Full-service restaurant with bar, 120 seats, no live entertainment",
         lines=["GL", "PROPERTY", "WC"], rev=(800_000, 3_000_000), pay_ratio=0.3,
         classes=[("9079", "Restaurant employees", 0.9), ("8810", "Clerical office employees", 0.1)],
         subs=None),
    dict(key="machine", word="Precision Machining", naics="332710",
         ops="CNC machining of aluminum and steel components for industrial customers",
         lines=["GL", "PROPERTY", "WC"], rev=(1_500_000, 8_000_000), pay_ratio=0.32,
         classes=[("3632", "Machine shop operations", 0.85), ("8810", "Clerical office employees", 0.15)],
         subs="Powder coating and heat treating sent out to vendors"),
    dict(key="janitorial", word="Cleaning Services", naics="561720",
         ops="Commercial janitorial services for offices and medical buildings",
         lines=["GL", "WC", "AUTO"], rev=(400_000, 2_500_000), pay_ratio=0.55,
         classes=[("9014", "Janitorial services", 0.9), ("8810", "Clerical office employees", 0.1)],
         subs=None),
    dict(key="autorepair", word="Auto Care", naics="811111",
         ops="General automotive repair and maintenance, no towing or body work",
         lines=["GL", "PROPERTY", "WC"], rev=(700_000, 2_800_000), pay_ratio=0.33,
         classes=[("8380", "Automobile service and repair", 0.85), ("8810", "Clerical office employees", 0.15)],
         subs=None),
    dict(key="bakery", word="Bakehouse", naics="311811",
         ops="Retail bakery producing breads and pastries sold on premises",
         lines=["GL", "PROPERTY", "WC"], rev=(350_000, 1_500_000), pay_ratio=0.34,
         classes=[("2003", "Bakery operations", 0.9), ("8810", "Clerical office employees", 0.1)],
         subs=None),
    dict(key="itconsult", word="Technology Partners", naics="541512",
         ops="IT consulting, network installation and managed services for small firms",
         lines=["GL", "WC"], rev=(800_000, 4_000_000), pay_ratio=0.5,
         classes=[("8810", "Clerical office employees", 0.7), ("8742", "Outside salespersons", 0.3)],
         subs="Low-voltage cabling subcontracted on larger projects"),
    dict(key="printing", word="Print Works", naics="323111",
         ops="Commercial offset and digital printing of marketing materials",
         lines=["GL", "PROPERTY", "WC"], rev=(600_000, 3_000_000), pay_ratio=0.3,
         classes=[("4299", "Printing operations", 0.8), ("8810", "Clerical office employees", 0.2)],
         subs=None),
    dict(key="courier", word="Courier Co", naics="492210",
         ops="Same-day local delivery of parcels and documents within the metro area",
         lines=["AUTO", "WC", "GL"], rev=(500_000, 2_500_000), pay_ratio=0.45,
         classes=[("7380", "Drivers and chauffeurs", 0.85), ("8810", "Clerical office employees", 0.15)],
         subs=None),
    dict(key="plumbing", word="Plumbing", naics="238220",
         ops="Residential plumbing repair, water heater and fixture installation",
         lines=["GL", "AUTO", "WC"], rev=(700_000, 3_500_000), pay_ratio=0.38,
         classes=[("5183", "Plumbing", 0.85), ("8810", "Clerical office employees", 0.15)],
         subs="Excavation for sewer line replacement subcontracted"),
]
VEHICLES = [("Ford", "F-250"), ("Ford", "Transit 250"), ("Chevrolet", "Silverado 2500HD"), ("Ram", "ProMaster 2500"),
            ("Toyota", "Tacoma"), ("Isuzu", "NPR HD"), ("Nissan", "NV200"), ("GMC", "Savana 3500")]
CLAIM_DESC = {
    "GL": ["Customer slip and fall at premises", "Property damage at client site", "Damage to customer vehicle",
           "Water damage to client property"],
    "AUTO": ["Rear-ended at intersection", "Backed into parked vehicle", "Side-swipe while changing lanes"],
    "WC": ["Employee back strain lifting equipment", "Laceration to hand", "Fall from ladder", "Knee injury on job site"],
    "PROPERTY": ["Kitchen fire, smoke damage", "Burst pipe, water damage", "Wind damage to roof", "Burglary, stolen equipment"],
}
CONSTRUCTION = ["Frame", "Joisted Masonry", "Non-Combustible", "Masonry Non-Combustible"]
VIN_CHARS = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"


def make_vin(rng: random.Random) -> str:
    body = [rng.choice(VIN_CHARS) for _ in range(17)]
    total = sum(_VIN_MAP[c] * w for c, w in zip(body, _VIN_WEIGHTS))
    check = total % 11
    body[8] = "X" if check == 10 else str(check)
    return "".join(body)


@dataclass
class Claim:
    claim_number: str
    date_of_loss: date
    line: str
    description: str
    paid: float
    reserve: float
    status: str


@dataclass
class Vehicle:
    year: int
    make: str
    model: str
    vin: str
    garaging_zip: str
    cost_new: float


@dataclass
class PayrollRow:
    state: str
    class_code: str
    description: str
    employees: int
    payroll: float


@dataclass
class Building:
    address: str
    city: str
    state: str
    zip: str
    building_value: float
    contents_value: float
    year_built: int
    construction: str


@dataclass
class Company:
    legal_name: str
    dba: str | None
    fein: str
    entity_type: str
    street: str
    city: str
    state: str
    zip: str
    years_in_business: int
    naics: str
    ops: str
    revenue: float
    payroll: float
    ft: int
    pt: int
    effective_date: date
    lines: list[str]
    prior_carrier: str
    prior_expiration: date
    contact_name: str
    contact_email: str
    contact_phone: str
    subcontractors_used: bool
    sub_explanation: str | None
    sub_cost: float | None
    valuation_date: date
    industry: str
    locations: list[str] = field(default_factory=list)  # full addresses
    claims: list[Claim] = field(default_factory=list)
    vehicles: list[Vehicle] = field(default_factory=list)
    payroll_rows: list[PayrollRow] = field(default_factory=list)
    buildings: list[Building] = field(default_factory=list)

    @property
    def mailing_address(self) -> str:
        return f"{self.street}, {self.city}, {self.state} {self.zip}"

    @property
    def losses_disclosed(self) -> bool:
        return bool(self.claims)


def _round(x: float, step: int) -> float:
    return float(int(round(x / step)) * step)


def make_company(rng: random.Random, as_of: date, used_names: set[str]) -> Company:
    ind = rng.choice(INDUSTRIES)
    entity = rng.choice(ENTITIES[:3] if rng.random() < 0.9 else ENTITIES)
    while True:
        prefix = rng.choice(PREFIXES)
        suffix = ENTITY_SUFFIX[entity]
        if entity == "Sole Proprietor":
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        else:
            word = ind["word"]
            name = f"{prefix} {word}" if word.endswith(suffix) else f"{prefix} {word} {suffix}".strip()
        if name not in used_names:
            used_names.add(name)
            break
    dba = f"{prefix} {ind['word']}" if entity == "Sole Proprietor" else (None if rng.random() < 0.8 else f"{prefix} Services")
    city, st, zip_, area = rng.choice(CITIES)
    street = f"{rng.randint(100, 9800)} {rng.choice(STREETS)}"
    fein = f"{rng.choice([20, 22, 26, 27, 31, 33, 35, 36, 38, 41, 45, 46, 47, 52, 54, 56, 61, 62, 74, 81, 83, 84, 86, 88])}-{rng.randint(1000000, 9999999)}"
    revenue = _round(rng.uniform(*ind["rev"]), 10_000)
    payroll = _round(revenue * ind["pay_ratio"] * rng.uniform(0.85, 1.15), 1_000)
    avg_wage = rng.uniform(42_000, 68_000)
    total_emp = max(2, int(round(payroll / avg_wage)))
    pt = rng.randint(0, max(0, total_emp // 4))
    ft = total_emp - pt
    eff = as_of + timedelta(days=rng.randint(20, 75))
    eff = date(eff.year, eff.month, 1) if rng.random() < 0.6 else eff
    if eff <= as_of:
        eff = as_of + timedelta(days=30)
    lines = list(ind["lines"])
    if "PROPERTY" not in lines and rng.random() < 0.15:
        lines.append("PROPERTY")
    if "AUTO" in lines and rng.random() < 0.25 and ind["key"] != "courier":
        lines.remove("AUTO")
    first, last = rng.choice(FIRST), rng.choice(LAST)
    domain = "".join(ch for ch in (prefix + ind["word"]).lower() if ch.isalnum())[:18] + ".example"
    subs = ind["subs"] is not None and rng.random() < 0.7
    valuation = as_of - timedelta(days=rng.randint(5, 45))
    co = Company(
        legal_name=name, dba=dba, fein=fein, entity_type=entity, street=street, city=city, state=st, zip=zip_,
        years_in_business=rng.randint(2, 35), naics=ind["naics"], ops=ind["ops"], revenue=revenue, payroll=payroll,
        ft=ft, pt=pt, effective_date=eff, lines=sorted(lines), prior_carrier=rng.choice(CARRIERS), prior_expiration=eff,
        contact_name=f"{first} {last}", contact_email=f"{first.lower()}.{last.lower()}@{domain}",
        contact_phone=f"({area}) 555-01{rng.randint(10, 99)}", subcontractors_used=subs,
        sub_explanation=ind["subs"] if subs else None, sub_cost=_round(revenue * rng.uniform(0.03, 0.12), 1_000) if subs else None,
        valuation_date=valuation, industry=ind["key"],
    )
    co.locations = [co.mailing_address]
    if rng.random() < 0.3:
        c2, s2, z2, _ = rng.choice(CITIES)
        co.locations.append(f"{rng.randint(100, 9800)} {rng.choice(STREETS)}, {c2}, {s2} {z2}")

    # claims - always before the loss-run valuation date
    n_claims = rng.choices([0, 1, 2, 3, 4], weights=[30, 30, 20, 12, 8])[0]
    prefix_map = {"GL": "GL", "AUTO": "AU", "WC": "WC", "PROPERTY": "PR"}
    for i in range(n_claims):
        ln = rng.choice(co.lines)
        dol = valuation - timedelta(days=rng.randint(40, 4 * 365))
        status = "Open" if rng.random() < 0.3 else "Closed"
        paid = _round(rng.uniform(800, 60_000), 50)
        reserve = _round(rng.uniform(2_000, 40_000), 100) if status == "Open" else 0.0
        co.claims.append(Claim(f"{prefix_map[ln]}-{dol.year}-{rng.randint(10000, 99999)}", dol, ln,
                               rng.choice(CLAIM_DESC[ln]), paid, reserve, status))
    co.claims.sort(key=lambda c: c.date_of_loss)

    if "AUTO" in co.lines:
        for _ in range(rng.randint(2, 7)):
            make, model = rng.choice(VEHICLES)
            co.vehicles.append(Vehicle(rng.randint(2015, as_of.year), make, model, make_vin(rng), co.zip,
                                       _round(rng.uniform(32_000, 78_000), 500)))
    # payroll rows that add up exactly
    total_emp = ft + pt
    classes = ind["classes"][: max(1, min(len(ind["classes"]), total_emp))]
    emps = [max(1, int(total_emp * share)) for _, _, share in classes[:-1]]
    while emps and sum(emps) > total_emp - 1:
        emps[emps.index(max(emps))] -= 1
    emps.append(total_emp - sum(emps))
    pays = [_round(payroll * share, 100) for _, _, share in classes[:-1]]
    pays.append(round(payroll - sum(pays), 2))
    for (code, desc, _), emp, pay in zip(classes, emps, pays):
        co.payroll_rows.append(PayrollRow(st, code, desc, emp, pay))
    if "PROPERTY" in co.lines:
        for loc in co.locations:
            street_part, city_part, rest = [p.strip() for p in loc.split(",")]
            s_, z_ = rest.split()
            co.buildings.append(Building(street_part, city_part, s_, z_, _round(rng.uniform(400_000, 2_500_000), 10_000),
                                         _round(rng.uniform(80_000, 600_000), 5_000), rng.randint(1955, 2018),
                                         rng.choice(CONSTRUCTION)))
    return co
