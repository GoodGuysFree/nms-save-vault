"""Curated, hand-reviewed lookup data (not generated; tools/gen_lookups.py reads CHAINS and
CHOICES to know which mission ids to export).

Sources: R6-facts-catalog.md 2.1 (chains: membership INFERRED from ids and table names),
2.3 (own base types), 2.5 (ship model folders, reward ship models from rewardtable.mbin,
the ShipClass enum), 2.6 (creature ids: the game has no English names for them).
"""

# Own base types (extraction time: a change bumps EXTRACTOR_VERSION).
OWN_BASE_TYPES = frozenset({
    "HomePlanetBase", "FreighterBase", "PlayerShipBase", "PlayerSpaceBase", "PlayerSpaceStationBase",
})
BASE_TYPE_LABELS = {
    "HomePlanetBase": "Planet base", "FreighterBase": "Freighter base", "PlayerShipBase": "Corvette layout",
    "PlayerSpaceBase": "Space base", "PlayerSpaceStationBase": "Space station base",
}


def _steps(prefix: str, first: int, last: int) -> tuple[str, ...]:
    return tuple(f"{prefix}{i}" for i in range(first, last + 1))


# Mission chains: key -> (label, mission ids in order).
CHAINS = {
    "artemis": ("Artemis Path", _steps("ACT1_STEP", 1, 13) + _steps("ACT2_STEP", 1, 13) + _steps("ACT3_STEP", 1, 4)),
    "atlas": ("Atlas Path", _steps("ATLAS", 1, 11)),
    "sentinel": ("Sentinels", _steps("SENTINELS_", 1, 5)),
    "autophage": ("Autophage", ("ROBOMISS_0", "ROBOMISS_1", "ROBOMISS_2", "ROBOMISS_4")),
    "titan": ("Titan worlds", ("WORLDSM0", "WORLDSM1", "WORLDSMA", "WORLDSMB")),
    "purple": ("Purple systems", _steps("PURPM", 1, 3)),
    "pirates": ("Pirates", _steps("PIRATES", 1, 3)),
    "water": ("Water story", _steps("WATERSTORY", 1, 5)),
    "living_ship": ("Living ship", _steps("BIO_SHIP", 1, 5)),
    "overseer": ("Overseer", _steps("OVERSEER", 1, 10)),
    "base_computer": ("Base computer archive", _steps("BASE_UPGRADE", 1, 21)),
}
# Choice missions: "started" means "chosen" (their final stage is 0).
CHOICES = {
    "EPILOGUE_NEWGAL": "new galaxy", "EPILOGUE_STAY": "stayed",
    "ART_DEAD": "Artemis dead", "ART_SIM": "Artemis simulated",
    "ART_SIM_TRUTH": "told the truth", "ART_SIM_LIE": "told a lie", "APOLLO_DEAD": "Apollo dead",
}
DONE_SENTINEL = 2147483647      # a Progress value seen on finished missions (INFERRED: done)

# Ship model -> type. Folders checked in this order (BIOPARTS sits inside S-CLASS).
SHIP_FOLDER_TYPES = (
    ("/BIGGS/", "Corvette"), ("/DROPSHIPS/", "Hauler"), ("/SHUTTLE/", "Shuttle"), ("/SCIENTIFIC/", "Explorer"),
    ("/SAILSHIP/", "Solar"), ("/SENTINELSHIP/", "Interceptor"), ("/BIOPARTS/", "Living ship"),
    ("/S-CLASS/", "Exotic"), ("/FIGHTERS/", "Fighter"),
)
# Fixed reward models (rewardtable.mbin GcRewardSpecificShip): stem -> (type, name).
SPECIAL_SHIP_MODELS = {
    "FIGHTERCLASSICGOLD": ("Fighter", "Golden Vector"),
    "FIGHTERSPECIALSWITCH": ("Fighter", "Horizon Vector NX"),
    "WRACERSE": ("Exotic", "Starborn Phoenix"),
    "WRACER": ("Exotic", "Starborn Runner"),
    "VRSPEEDER": ("Fighter", "Utopia Speeder"),
    "SPOOKSHIP": ("Explorer", "Boundary Herald"),
    "BIOFIGHTER": ("Living ship", "The Wraith"),
    "RASAMAMAGOLD": ("Fighter", "Golden Rasamama S36"),
    "VINTAGEINTERCEPTOR": ("Interceptor", "Vintage Interceptor"),
    "COCKPIT_A": ("Exotic", None),          # under S-CLASS/ROYALPARTS; unexplained, shown as Exotic
}
# The game's ShipClass enum (stored on archived ships) -> display name.
SHIP_CLASS_NAMES = {
    "Fighter": "Fighter", "Dropship": "Hauler", "Scientific": "Explorer", "Shuttle": "Shuttle",
    "Royal": "Exotic", "Alien": "Living ship", "Sail": "Solar", "Robot": "Interceptor",
    "Corvette": "Corvette", "Freighter": "Freighter", "SwarmDrone": "Swarm drone",
}

# Companion creature id -> friendly type (hand-made; ids seen in real saves).
CREATURE_TYPES = {
    "FLYINGLIZARD": "Flying lizard", "FLYINGSNAKE": "Flying snake", "TRICERATOPS": "Triceratops",
    "ROBOTANTELOPE": "Robot antelope", "GRUNT": "Grunt", "STRIDER": "Strider", "CAT": "Cat",
    "FIEND": "Fiend", "LARGEBUTTERFLY": "Large butterfly", "LAND_JELLYFISH": "Land jellyfish",
    "WALKINGBUILDING": "Walking building", "BONECOW": "Bone cow", "ROBO_RODENT": "Robot rodent",
    "SIXLEGCOW": "Six-legged cow", "SPIDERQUAD_PET": "Spider quad", "QUAD_PET": "Quad",
    "TREX": "T-rex", "FLYINGBEETLE": "Flying beetle", "RODENT": "Rodent",
    "TWOLEGANTELOPE": "Two-legged antelope", "PURPLE_WEIRD": "Purple weird", "COW": "Cow",
    "ROBO_PET": "Robot pet", "BONECAT": "Bone cat", "SANDWORM": "Sandworm", "SPIDER": "Spider",
    "ARTHROPOD": "Arthropod", "ANTELOPE": "Antelope", "BIRD": "Bird", "BLOB": "Blob",
}
