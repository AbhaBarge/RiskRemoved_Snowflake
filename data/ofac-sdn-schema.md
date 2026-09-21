# OFAC SDN XML schema reference

Source: [OFAC SDN XML download](https://www.treasury.gov/ofac/downloads/sdn.xml). The endpoint redirects to OFAC's Sanctions List Service and is updated in place. This reference covers the legacy `SDN.XML` structure (the file commonly called `sdn.xml`), rather than the separate `SDN_ADVANCED.XML` format.

## How paths are written

Paths are absolute, starting at `/sdnList`. `[*]` means a repeated element. In Snowflake-style XML notation, `XMLGET(parent, 'tag', n)` retrieves the zero-based occurrence `n`; flatten each `[*]` level before extracting scalar text with `GET(node, '$')`.

## Document envelope

| Path | Occurs | Meaning |
| --- | ---: | --- |
| `/sdnList/publshInformation/Publish_Date` | 1 | Publication date supplied in the list. (`publshInformation` is intentionally misspelled in the source schema.) |
| `/sdnList/publshInformation/Record_Count` | 1 | Number of `sdnEntry` records published. |
| `/sdnList/sdnEntry[*]` | 0..n | One sanctions-list subject. |

Example root extraction:

```sql
GET(XMLGET(doc, 'sdnList'), 'publshInformation')
```

## Subject (`sdnEntry`) fields

Flatten `/sdnList/sdnEntry[*]` first. The following child paths are relative to each entry.

| Relative XML path | Typical value / purpose |
| --- | --- |
| `uid` | Stable numeric OFAC subject identifier. |
| `lastName` | Primary name; for an entity, generally its display name. |
| `firstName` | Given name, when applicable. |
| `title` | Honorific or position, when provided. |
| `sdnType` | Subject type, such as `Individual`, `Entity`, `Vessel`, or `Aircraft`. |
| `remarks` | Free-text explanatory material. |
| `programList/program[*]` | OFAC sanctions program code(s). |

Illustrative XMLGET expressions after the entry is bound as `e`:

```sql
GET(XMLGET(e, 'uid'), '$')::string       AS uid,
GET(XMLGET(e, 'lastName'), '$')::string  AS last_name,
GET(XMLGET(e, 'sdnType'), '$')::string   AS sdn_type
```

## Repeating child structures

| Path from entry | Fields beneath each repeated item |
| --- | --- |
| `akaList/aka[*]` | `uid`, `type`, `category`, `lastName`, `firstName` — aliases and alternate identities. |
| `addressList/address[*]` | `uid`, `address1`, `address2`, `address3`, `city`, `stateOrProvince`, `postalCode`, `country`, `addressRemarks`. |
| `nationalityList/nationality[*]` | `uid`, `country`. |
| `citizenshipList/citizenship[*]` | `uid`, `country`. |
| `dateOfBirthList/dateOfBirthItem[*]` | `uid`, `dateOfBirth` — can hold a full or partial date expression. |
| `placeOfBirthList/placeOfBirthItem[*]` | `uid`, `placeOfBirth`. |
| `idList/id[*]` | `uid`, `idType`, `idNumber`, `idCountry`, `issueDate`, `expirationDate`, `idRemarks`. |

The `idList/id[*]` entries are the structured identifying-document and registration attributes (for example passports, national IDs, tax IDs, or business registrations). Do not assume an `idNumber` is unique across entries or identifier types.

## Vessel-only extension

When `sdnType` is `Vessel`, `vesselInfo` may be present:

| Path from entry | Meaning |
| --- | --- |
| `vesselInfo/callSign` | Maritime call sign. |
| `vesselInfo/vesselType` | Vessel classification. |
| `vesselInfo/vesselFlag` | Flag state. |
| `vesselInfo/vesselOwner` | Owner text. |
| `vesselInfo/tonnage` | Tonnage value. |
| `vesselInfo/grossRegisteredTonnage` | Gross registered tonnage. |

## Relational extraction shape

Use one `sdn_entry` table keyed by `/sdnList/sdnEntry/uid`, then child tables keyed by that UID plus each child item's own `uid` where supplied. Preserve original text exactly; missing tags are normal and should become `NULL`, not empty strings. A practical normalized layout is:

```text
sdn_entry(uid, last_name, first_name, title, sdn_type, remarks)
sdn_program(sdn_uid, program)
sdn_alias(sdn_uid, aka_uid, type, category, last_name, first_name)
sdn_address(sdn_uid, address_uid, ...)
sdn_nationality(sdn_uid, nationality_uid, country)
sdn_citizenship(sdn_uid, citizenship_uid, country)
sdn_birth_date(sdn_uid, birth_date_uid, date_of_birth)
sdn_birth_place(sdn_uid, birth_place_uid, place_of_birth)
sdn_identifier(sdn_uid, identifier_uid, id_type, id_number, ...)
```

## Notes

- XML element names are case-sensitive; use the exact spellings above.
- The SDN list is dynamic. Use `Publish_Date` and `Record_Count` as ingestion metadata and retain the source file/version with every load.
- `SDN_ADVANCED.XML` has a different, richer data model. Do not apply these paths to it without a separate schema mapping.
