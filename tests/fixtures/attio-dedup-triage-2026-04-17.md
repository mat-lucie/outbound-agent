# Attio Dedup — Conflict Triage

<!-- Anonymized snapshot: prospect LinkedIn slugs, names, and email
     addresses replaced with synthetic values (2026-06-10 public-repo
     scrub). Structure, group counts, shapes, and CRM UUIDs are
     unchanged so the §3.11 union-merge parser tests stay valid. -->

Generated from scan at `2026-04-17T14:49:22.612142Z`.
Total conflict groups: **68**.

## Counts by conflict dimension

| Dimension | Groups affected |
|---|---|
| `entry.persona` | 59 |
| `company` | 19 |
| `email` | 7 |
| `name` | 4 |

## Counts by exact conflict shape (multiple dimensions combined)

| Shape | Groups | Approve path |
|---|---|---|
| `entry.persona` | 45 | Usually safe to approve — newer weekly run picked better persona; losing old value acceptable |
| `company+entry.persona` | 13 | REVIEW |
| `company` | 2 | REVIEW — prospect may have changed companies; decide which company is current |
| `email+name` | 2 | REVIEW — different names on same LinkedIn URL; verify manually |
| `email` | 2 | REVIEW |
| `company+email` | 2 | REVIEW — prospect may have changed companies; decide which company is current |
| `company+email+name` | 1 | REVIEW — prospect may have changed companies; decide which company is current |
| `company+entry.persona+name` | 1 | REVIEW — different names on same LinkedIn URL; verify manually |

---

## Shape: `entry.persona` (45 groups)

### https://www.linkedin.com/in/prospect-001

- **winner_id**: `016e7adb-efa0-4f3f-bb70-1032870b4e41`
- **loser_ids**: `33dcae73-6b67-4527-9651-88fc1bd807d1, 72ab9337-6542-4939-9f3a-d5f1aaccb000, 74f5f1e0-4b8a-4047-8dff-cca121d67e12, af0c98ad-04d4-4ae0-a8a9-460485b096e3`
- **conflict_reasons**:
  - entry.persona: d2dc2adb='digitalization_champions', 8bf1f58f='operations_leaders'
- **list_entry_actions**: 5 total

### https://www.linkedin.com/in/prospect-002

- **winner_id**: `0b236238-cb7f-4516-a23f-9cd842d95555`
- **loser_ids**: `028f82e7-1167-4b19-a9dd-00b873cf225e, 056a1c2b-b440-4edf-a0b5-0f1040a2378c, ac8770f7-5dea-414d-833b-ea02360a861c, fc8021e3-cf51-40d8-8215-8e8421bfdda0`
- **conflict_reasons**:
  - entry.persona: a09359c9='operations_leaders', abda3e36='digitalization_champions'
- **list_entry_actions**: 5 total

### https://www.linkedin.com/in/prospect-003

- **winner_id**: `07867e17-3817-4339-aa40-96ea2d6a72ed`
- **loser_ids**: `02e1257f-d0a4-4488-a1b4-de1d54c371f2, 3088519a-1705-43db-9522-c97593919415, 720145d1-4c09-46ca-aa4f-a01f2b92ef93`
- **conflict_reasons**:
  - entry.persona: 866065f8='cl_midmarket_manufacturing', 8e9a30b6='operations_leaders'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-004

- **winner_id**: `1d10cffc-f138-431d-b5fe-f644ba991d97`
- **loser_ids**: `07e1c32f-331a-4737-87ce-a5b94a467f12, 92dc79c8-754a-49a7-b17e-73f74f779950, 98daa512-b366-48df-aa05-b67d29f76148, c53d9fd2-29dd-4426-9e87-c729b9c9aeb9`
- **conflict_reasons**:
  - entry.persona: 998ec72c='operations_leaders', fad51c13='digitalization_champions'
- **list_entry_actions**: 5 total

### https://www.linkedin.com/in/prospect-005

- **winner_id**: `26b5aacb-d309-4415-abb7-b25d2893e34a`
- **loser_ids**: `0889f918-2816-43d2-8fc0-7d0b0a530f40, 2bf0f081-7039-49b1-b03d-b7d1a86ea943, 3db53a7f-f356-402b-afbf-c146e9ab74b6, 57874da1-9275-41a1-aa87-fb604003521f`
- **conflict_reasons**:
  - entry.persona: 440f96db='operations_leaders', 79686e25='digitalization_champions'
- **list_entry_actions**: 5 total

### https://www.linkedin.com/in/prospect-006

- **winner_id**: `482df030-2257-4cb7-97d9-def06e2a557c`
- **loser_ids**: `0a20eb16-59cd-4e59-8df7-bbb337fec9f3, 27c212de-06c4-4aa9-8fbd-0f3f2d45e560, 3ae226cf-8606-4ed6-a6bb-f85e1c835d88, d2eee4fe-409b-4f89-8b4b-41c206505eb5`
- **conflict_reasons**:
  - entry.persona: fe48c148='operations_leaders', 7fcf3c70='digitalization_champions'
- **list_entry_actions**: 5 total

### https://www.linkedin.com/in/prospect-007

- **winner_id**: `532f7f32-fc79-40f2-8c31-fce44368aff9`
- **loser_ids**: `0d872fb7-2a5d-4f14-8b56-1ffe91499a7a, 17bc8521-6e38-4110-897a-895462b2d0c0, 62cbdb13-76c1-4d0b-b374-a29832171750`
- **conflict_reasons**:
  - entry.persona: dd6b093a='co_midmarket_manufacturing', 648c4e1a='operations_leaders'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-008

- **winner_id**: `9a4d1022-373c-4610-9955-8bf8f6d8a6ba`
- **loser_ids**: `0e8ef0d8-1be2-4034-abf7-b8c971b33556, 394a20b0-ce1b-4051-b66c-5ecd2fbf0b95, 5d6183c3-3d4c-4a3c-96d3-96cd782cb063`
- **conflict_reasons**:
  - entry.persona: 51e1e691='operations_leaders', 5241fd8f='digitalization_champions'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-009

- **winner_id**: `1af54902-9f48-4259-9e84-2e2fbe2131b9`
- **loser_ids**: `0ff89c27-903d-404a-86ea-e09ef586a92c, 66082d37-8730-432b-92ac-27597e5e4d89, c71b5f87-ef4c-4ac6-beb5-717e3e0e8dfd`
- **conflict_reasons**:
  - entry.persona: 9a6db796='operations_leaders', b2b13c69='digitalization_champions'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-010

- **winner_id**: `17813f9e-9e54-4234-9007-6fc1d2120873`
- **loser_ids**: `12e63623-47b3-42e4-a9b4-57333c8b818b, 192d9429-ea91-4423-9bb6-f8767ac5817a, 1cf7b807-9300-448d-8e61-eab87ad2d884, 2cb46cf7-5db6-4faa-93ab-8fc4f24ffaf0`
- **conflict_reasons**:
  - entry.persona: a694bbe2='operations_leaders', eae474ca='digitalization_champions'
- **list_entry_actions**: 5 total

### https://www.linkedin.com/in/prospect-011

- **winner_id**: `12f067a6-26d5-417c-95ca-5ebce586391e`
- **loser_ids**: `4fefdcbc-59b1-41f1-9b05-6c3ac0b7bc63, 50178348-0423-498c-9691-8efd410e7707, ef4510a2-16c7-42be-969d-9101952c2dd5`
- **conflict_reasons**:
  - entry.persona: d3403fa1='mx_midmarket_manufacturing', b79bdeb9='operations_leaders'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-012

- **winner_id**: `a9712824-c744-4689-a3a4-d916fe8a64ca`
- **loser_ids**: `132dc670-0360-4473-bb31-f5d4de66aff6, 4f93628d-6b2c-4755-be01-febcabd0f4eb, 9912340d-a777-440e-90cc-2eca83dfef5a, d86300d1-383b-4b4f-bccf-e23905efbba9`
- **conflict_reasons**:
  - entry.persona: 7cd8bd36='operations_leaders', 77cab30d='digitalization_champions'
- **list_entry_actions**: 5 total

### https://www.linkedin.com/in/prospect-013

- **winner_id**: `13d62301-0cb0-47fe-b781-1469a5f28fea`
- **loser_ids**: `21f85e8a-edb3-4bf7-8609-cb76e5bfb4d0, 5a796c87-2c9b-42e0-9381-9c9fe0f5d777, 774e9679-2b73-4ce9-9fc2-84f421e14c85`
- **conflict_reasons**:
  - entry.persona: e889ad91='mx_midmarket_manufacturing', 61ec7f26='operations_leaders'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-014

- **winner_id**: `ca4b11b1-e86c-4b0d-8dc9-aa560a731911`
- **loser_ids**: `1550b42f-6adb-483b-a1ac-3c0b13c1203c, 9094e94e-3357-4a03-af29-a98e3d7df5b8, 9d541ede-a7c9-425e-bd7d-96edcd14fd65`
- **conflict_reasons**:
  - entry.persona: 661f82b3='operations_leaders', c63b54db='co_midmarket_manufacturing'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-015

- **winner_id**: `f30ee392-f18b-48de-8627-3c777525b14e`
- **loser_ids**: `164dabc4-abae-4612-bdbb-5490b5350a7b, 9940f072-16fc-4361-bb67-49eacb335aff, 9fe695fa-e05b-4f16-9411-f586459f2a87, b84c6dc9-9768-4bfb-aef0-fd83010e53ee`
- **conflict_reasons**:
  - entry.persona: edf46a40='operations_leaders', f155a827='digitalization_champions'
- **list_entry_actions**: 5 total

### https://www.linkedin.com/in/prospect-016

- **winner_id**: `e39c7330-93d1-4b82-bbac-17e9492dcee5`
- **loser_ids**: `17431ae8-2d87-46d4-bd68-3d53cb470abb, 33f5eeb5-2ecc-4fd9-a952-6253b78e325d, 5d9b94b8-d435-45fb-ab82-af28ee391fde, a007d891-0d1a-4a2f-9eee-479fd7245cc4`
- **conflict_reasons**:
  - entry.persona: bf2847c6='operations_leaders', a6ecf800='digitalization_champions'
- **list_entry_actions**: 5 total

### https://www.linkedin.com/in/prospect-017

- **winner_id**: `c02ac74a-c8f7-47c6-a309-023c6b4bc753`
- **loser_ids**: `1a057e34-4db6-445a-8218-3397602c66a0, acb419d6-d3ee-4d54-a078-8244e1614633, b819c05f-6a4f-4bdd-ae86-145a82f833a6`
- **conflict_reasons**:
  - entry.persona: 022eb6de='operations_leaders', 77fcac78='digitalization_champions'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-018

- **winner_id**: `3cfc77a7-16fa-4cd1-a23d-807681cbb390`
- **loser_ids**: `1b59abef-b00c-4256-bccb-ecbecea92b04, 57633785-4899-4470-ac55-cadc692fbc06, 584b8fa7-a99b-4ab4-b777-8aef500bd6e4`
- **conflict_reasons**:
  - entry.persona: d56f3f41='operations_leaders', d5dfe869='digitalization_champions'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-019

- **winner_id**: `bd0dd07c-9d56-446a-8935-8b674ac686c5`
- **loser_ids**: `1d1ac95b-5706-44f7-a369-b98fe4086f69, 37e26167-9fde-4468-bf67-b5bdc44acbe8, aa2022cb-7c4f-4c23-b016-58a3f35f60b8, aa2d3eb3-2d30-4971-96e6-bfa94d1f3899`
- **conflict_reasons**:
  - entry.persona: 2c118116='operations_leaders', 6d81adf3='digitalization_champions'
- **list_entry_actions**: 5 total

### https://www.linkedin.com/in/prospect-020

- **winner_id**: `1ff6bdca-bcb2-4518-8db9-125b09b1119e`
- **loser_ids**: `6eb71464-8919-487e-bfa4-9e3c6af51d1a, 78d6955f-e549-487f-9f41-3feccaa309be, ea1fc8a4-c249-491b-87b4-7deb22d633cf`
- **conflict_reasons**:
  - entry.persona: 597b8952='operations_leaders', 9a54ff3b='digitalization_champions'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-021

- **winner_id**: `4e48f1e1-3841-45a0-b456-5a756212129c`
- **loser_ids**: `2391a87e-1aa7-4611-ae4d-f29de27c5bf6, 8787f47f-ba58-42d3-810f-6e336e445aaa, e85f4f54-ab4c-43ba-8861-b2167dc6e418`
- **conflict_reasons**:
  - entry.persona: 7691dd5b='operations_leaders', 030484a1='digitalization_champions'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-022

- **winner_id**: `47c0b8c9-2fde-4c27-826d-ff13595639f4`
- **loser_ids**: `26f40cc7-8635-4080-8103-d0626531efbd, 510ed1e8-8bb8-4c0b-ba0f-0db18db4ed11, a30026f6-1f80-4b92-83bd-eb93c58f4db8, daa937ea-be8d-43d4-b8ef-9825c2d0a895`
- **conflict_reasons**:
  - entry.persona: 027f7ef5='digitalization_champions', ede8dd6d='operations_leaders'
- **list_entry_actions**: 5 total

### https://www.linkedin.com/in/prospect-023

- **winner_id**: `3fad270b-d39b-4e57-ac5e-13eac2fb2258`
- **loser_ids**: `28538a3e-3aae-4efb-8390-2e7301cf988d, 29e1e4ec-d704-4d6a-b512-cb7abe301439, 647ce17c-bd2e-4ba5-8e3e-81f126a20868, d9ca1da3-c352-4f53-81ee-98cd05ab65ec`
- **conflict_reasons**:
  - entry.persona: ec80a5f8='operations_leaders', a1a103c0='digitalization_champions'
- **list_entry_actions**: 5 total

### https://www.linkedin.com/in/prospect-024

- **winner_id**: `37b62169-0aca-45ca-8a85-2e63397408d4`
- **loser_ids**: `2b065c65-2729-4004-867f-1ac76d8687e1, 4d0f9080-4768-48de-8b44-950e91a79b07, 5547746c-bfcf-49d9-90fb-44498cbcdbc8, 97514ceb-060a-41d1-a322-526ddf03d0d7`
- **conflict_reasons**:
  - entry.persona: dac24793='operations_leaders', 75ca00e7='digitalization_champions'
- **list_entry_actions**: 5 total

### https://www.linkedin.com/in/prospect-025

- **winner_id**: `a3348257-8fc2-45f6-ad11-810f1f8faa51`
- **loser_ids**: `2d93fa38-eb4b-46b9-b8b2-9faeca486866, 7b2636d5-d179-41a7-8aa5-d2631bf72e36, 89897767-ea6c-4ed2-8ba1-ef72dcb97e35`
- **conflict_reasons**:
  - entry.persona: e898f258='operations_leaders', cc638206='digitalization_champions'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-026

- **winner_id**: `2f95f5e4-62df-40a5-b70b-2e7af7209901`
- **loser_ids**: `3f6c0f22-cba7-4d0b-a1f9-f9357c158a43, 67c102af-4eae-47f5-9006-2132fb069577, bebdfbca-97d2-4475-aee5-e184927c8a2a`
- **conflict_reasons**:
  - entry.persona: 7b0db642='mx_midmarket_manufacturing', e762c49a='operations_leaders'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-027

- **winner_id**: `5fec40e3-fb8b-4a52-8bf3-8e241be21355`
- **loser_ids**: `37e1ed54-6e2b-4e8d-8c9e-36e588358e99, 63ffa2ec-8204-4a6a-af0f-9812269fa331, 88d9641a-1384-48ba-8ef9-755ffadf4452, aeefd76b-1c07-4ccb-8d39-d1083221ba22`
- **conflict_reasons**:
  - entry.persona: 51a145aa='operations_leaders', e23ba1e4='digitalization_champions'
- **list_entry_actions**: 5 total

### https://www.linkedin.com/in/prospect-028

- **winner_id**: `4352f46d-fed6-408c-bb60-b3ab2e310aaa`
- **loser_ids**: `56b85392-9e09-495f-b86b-34b766a4f830, 875defd4-5ff6-49f6-80cc-d80b0942173e, b735a78e-33e4-4a98-abeb-6f5d32994ee3`
- **conflict_reasons**:
  - entry.persona: c9015b89='operations_leaders', 2b62a425='digitalization_champions'
- **list_entry_actions**: 5 total

### https://www.linkedin.com/in/prospect-029

- **winner_id**: `45987b14-dbe5-43d1-baf6-b7ab22d88e4c`
- **loser_ids**: `8067d439-7450-4906-aca9-88248291effc, a8cc2971-6407-45df-9900-382fd7a784f0, ccae4303-58be-4532-8559-13aeb180001c`
- **conflict_reasons**:
  - entry.persona: ce4b7b76='digitalization_champions', d78681bf='operations_leaders'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-030

- **winner_id**: `4f4f1a85-54bb-4ff4-9a6d-da9c272f2acc`
- **loser_ids**: `6290fc76-3660-4cfd-8f03-02bddd97c846, 7a0c2a54-71f6-45b7-b77e-f0e4fcbbf594, a34d72fa-4e0a-4090-a9a2-ae347a31d627, c09bf432-4034-49f7-ac9a-1afbe8994652`
- **conflict_reasons**:
  - entry.persona: 6a8a6652='operations_leaders', 88d371fb='digitalization_champions'
- **list_entry_actions**: 5 total

### https://www.linkedin.com/in/prospect-031

- **winner_id**: `b31980ed-86ac-4438-ae08-9389e5cc930e`
- **loser_ids**: `50b64402-f47d-4b7d-89f0-60c251a9b62e, 9b6a721c-f6e0-4d39-b021-847dcdfa7362, ccc08b89-e8f7-4e49-9e1d-644ba36d1ec5`
- **conflict_reasons**:
  - entry.persona: 9317266b='operations_leaders', dd2049e4='digitalization_champions'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-032

- **winner_id**: `81317ec7-048a-4dc3-b36f-cb425336d620`
- **loser_ids**: `581c4495-3c8c-4e9b-81f0-d204f33cdbfd, add0aeae-e74b-4066-a504-11a0ab4a82c1, d6f3ee68-3737-4c52-b2c8-7606340b9cf5`
- **conflict_reasons**:
  - entry.persona: 6cb04165='cl_midmarket_manufacturing', 93646339='operations_leaders'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-033

- **winner_id**: `709fe96c-3732-4e20-b538-8882b50497b1`
- **loser_ids**: `591ec329-02fc-447a-931e-b46e7cc04275, 5cab43c3-fa92-45f6-ab2b-92372aa163b0, d20292aa-2996-4994-9aef-a3ad495899a7`
- **conflict_reasons**:
  - entry.persona: 859affc7='mx_midmarket_manufacturing', 32e72ba7='operations_leaders'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-034

- **winner_id**: `b4ec75a7-44fb-416e-ab6b-203b4ea8fd80`
- **loser_ids**: `5c09da54-ac8c-49ee-8080-2b3fbc3c109d, 63b17f13-e78a-4950-97fe-2397a807787c, a11377cb-2bd4-4214-b225-a21b36fcf993`
- **conflict_reasons**:
  - entry.persona: 4d5e9521='co_midmarket_manufacturing', 8ff6112c='operations_leaders'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-035

- **winner_id**: `c760bd9a-f9b2-4c49-a0ef-3f4978dd43d6`
- **loser_ids**: `5e5fe653-8964-42a2-a18f-628f621e419b, c7442896-0685-48c4-bc32-4f4393f31b75, fb7e7d9e-c870-4d3d-8994-c9fff6f083f3`
- **conflict_reasons**:
  - entry.persona: fcb52fa4='operations_leaders', cd6a10f6='digitalization_champions'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-036

- **winner_id**: `5f876fa8-f4e4-4be6-a909-8f4c029a6750`
- **loser_ids**: `66ad0365-2e05-454f-a8ab-5b0089960dcb, 9731861b-01c5-4ebb-b9ac-31aa4541fbe2, ea206779-1559-4efb-b6d4-844a037443c7`
- **conflict_reasons**:
  - entry.persona: 3fcf5f12='operations_leaders', b6a69b6a='digitalization_champions'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-037

- **winner_id**: `6ef7ee21-86dd-4a1a-8876-d3d1020676e0`
- **loser_ids**: `5fdf69cd-80f7-47ca-9dd7-d0d3b107e536, b4429cbc-b0db-45db-a161-ba0604d52cef, cb02243a-7bae-4dd5-94b7-a56dbb28c87a`
- **conflict_reasons**:
  - entry.persona: 5c942ebe='cl_midmarket_manufacturing', 61cf5217='operations_leaders'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-038

- **winner_id**: `dbc84419-8546-44d6-8230-af6904e9e8ae`
- **loser_ids**: `64ddfca0-ec6d-4000-a1a8-14b14f2de130, d5cf1755-ee3a-4c82-9b61-e6a15aacba3f, f94d925b-c4e8-4224-a4e5-354f0f8014f1`
- **conflict_reasons**:
  - entry.persona: ecc07ba7='operations_leaders', 194635f3='digitalization_champions'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-039

- **winner_id**: `74dd966e-89bd-447a-8ae8-86aa94053474`
- **loser_ids**: `8f3adb78-d091-4ff9-9ef0-dcb35418649d, dc280ada-8a22-49f4-aa24-1b0e1fa3f6f0, f4aebfc2-8dbe-40dd-ad18-b53a4cb7eb42, f5080967-7e94-4342-92dc-e392c43fea26`
- **conflict_reasons**:
  - entry.persona: 556b0837='digitalization_champions', 68257c1c='operations_leaders'
- **list_entry_actions**: 5 total

### https://www.linkedin.com/in/prospect-040

- **winner_id**: `74ee856a-f8fd-4a4b-932b-2d6172f3afc0`
- **loser_ids**: `917a1183-8666-4df8-9de3-8e0a949831a2, aab7c79a-b372-4395-8c12-5e1343afbd56, b4cd89c7-7afb-43c3-b3db-5f89b4069627`
- **conflict_reasons**:
  - entry.persona: a018aa89='digitalization_champions', 42edca72='operations_leaders'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-041

- **winner_id**: `a5a5336f-954a-42da-82d9-5d3708fcc1f0`
- **loser_ids**: `7dc8a363-2940-47ea-b28f-f2ed45756ad2, a0dc275d-1618-44ba-8196-3fa02f74c33a, c96c6606-18fb-4e70-9d96-4fb9cadeeb92, efdc4691-95c7-45c5-90de-4a76f595f9d8`
- **conflict_reasons**:
  - entry.persona: 73218c11='operations_leaders', 97fa0636='digitalization_champions'
- **list_entry_actions**: 5 total

### https://www.linkedin.com/in/prospect-042

- **winner_id**: `7ea16d59-9858-47c6-a1ae-78304fefcc54`
- **loser_ids**: `a014f1e7-5f3e-4fc6-90f3-960daa3c8380, a9c39cae-17e8-414e-a09d-e9c2d7fec677, c6bb06d1-92cb-4841-90f4-91c56b534ee2`
- **conflict_reasons**:
  - entry.persona: 39cc684c='operations_leaders', 1af5e4b6='digitalization_champions'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-043

- **winner_id**: `ac851e96-2c6d-40f1-adc5-8c4ed3181e0c`
- **loser_ids**: `9567afeb-108e-44a8-ab07-6ec1da0429f5, f8ad1d3f-a7f2-41d6-9135-aaad04534131, fd1be837-3aa2-447b-b8df-e218f431eee2`
- **conflict_reasons**:
  - entry.persona: 2c98da10='mx_midmarket_manufacturing', b6e25773='operations_leaders'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-044

- **winner_id**: `cc46c4df-5894-4736-b1cb-cfcde5d88966`
- **loser_ids**: `9695557f-019c-45fd-9af1-48c8f542f748, a65df254-19fd-4ee5-8bf6-640a72c5d858, bc28c02d-90f9-4fb6-8af5-66f8dab86e40`
- **conflict_reasons**:
  - entry.persona: e930c584='cl_midmarket_manufacturing', 6bbc0266='operations_leaders'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-045

- **winner_id**: `ac1dd04e-942c-4b2b-8639-8ebdfb4611b5`
- **loser_ids**: `b6700ed5-26b7-4797-84f5-6dfbed6a863f, bc55debd-fc32-4aef-9135-95af698c4ead, dcbcba9c-e691-45fa-8e0f-76de474b19fa`
- **conflict_reasons**:
  - entry.persona: ebb02d72='cl_midmarket_manufacturing', 86fb9cd1='operations_leaders'
- **list_entry_actions**: 4 total

---

## Shape: `company+entry.persona` (13 groups)

### https://www.linkedin.com/in/prospect-046

- **winner_id**: `92e7b7b9-350d-4ba6-9d74-1a573aaf4376`
- **loser_ids**: `052454df-3f2c-4e1b-a2b4-8741a9491dc4, 0729abb7-a276-477f-973a-247ebbfd2b37, 2c747ae7-3729-44a8-84cd-df9f5b5fd1da, b1732735-8b1c-40c3-8b8f-db3aa282bea6`
- **conflict_reasons**:
  - company: 052454df='6ee0e3de-a050-5d57-84ec-bd1d0c2b51ff', 92e7b7b9='b2bfa1f9-6537-5d69-85b4-8d3a4c394b99'
  - entry.persona: 57ab556e='operations_leaders', 80d35a25='digitalization_champions'
- **list_entry_actions**: 5 total

### https://www.linkedin.com/in/prospect-047

- **winner_id**: `205b0413-3176-4e89-952c-ff71ffb537f7`
- **loser_ids**: `1012ac7e-2a42-4695-88a6-ba95faf932a0, 5742d32f-42a8-4063-b41f-3f043a6acf8c, 728db429-a1b2-49f6-89ea-81ec47c8614f, fa44817a-27f9-439d-ba53-df24120025a0`
- **conflict_reasons**:
  - company: 1012ac7e='109e28e8-7833-4164-a5e8-79e32ab23fdb', 205b0413='328d4e59-baef-4f02-9021-5ca4483c1f29'
  - entry.persona: 311cdfb8='digitalization_champions', f1485f46='operations_leaders'
- **list_entry_actions**: 5 total

### https://www.linkedin.com/in/prospect-048

- **winner_id**: `f79635c9-aec6-4b63-b2ee-c5b5de8127e8`
- **loser_ids**: `12455e51-bafa-450f-9fdb-efe7d7f31d69, 15674c0b-5ab6-437e-9093-72a11010b7a6, a07cfade-62dd-4c5d-9dd2-f22c278e5b77`
- **conflict_reasons**:
  - company: 12455e51='f531f384-8201-4a26-bfd0-fb101788132f', f79635c9='62914093-43b7-41ef-953b-4765347b4580'
  - entry.persona: 8b165833='operations_leaders', ab3978a7='digitalization_champions'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-049

- **winner_id**: `169dd1a2-a97e-48c5-9ec1-6059b3252e2f`
- **loser_ids**: `275d5646-d737-4289-b272-bc3507fdcb8d, d11aa41d-df70-4380-94be-3aae08b5e599, e449e738-f2fc-47bd-9f1c-cca0828dd867`
- **conflict_reasons**:
  - company: 169dd1a2='54fa12b7-71af-4814-b20a-3ca66b2dc0d5', 275d5646='99fc5736-7fbd-4065-9c19-8f1d83ec6546'
  - entry.persona: fc6f83c1='operations_leaders', 192c9998='cl_midmarket_manufacturing'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-050

- **winner_id**: `3399936e-0454-4214-a960-7dc49847518e`
- **loser_ids**: `17168a34-ed0f-4c53-893a-f89b5b91dacb, 59b8d813-d2f7-4ab9-ae16-01c59ea94ef7, 90a9f60f-5f62-437d-a775-66d9f8e681e1, b1909d19-1d26-4957-89a6-dc10cd6ebc5e`
- **conflict_reasons**:
  - company: 17168a34='3c29c5bb-94f4-4f4d-94d2-341f9bccb356', 3399936e='daffae2c-df8b-5519-b95e-f729c5fd86c2'
  - entry.persona: 4441e7c5='digitalization_champions', 4c33a314='operations_leaders'
- **list_entry_actions**: 5 total

### https://www.linkedin.com/in/prospect-051

- **winner_id**: `c4a05e7a-c43e-4f72-97a4-25ddd09393dc`
- **loser_ids**: `1aed6936-d03d-4361-b632-cd0dd1ab57dd, 42751728-0906-4785-9bd2-8e00d6cc407b, a767cf38-c9ee-441e-8bfa-0e9cf520ce88`
- **conflict_reasons**:
  - company: 1aed6936='c68530b0-3471-40a7-92d8-b283ba573b1f', c4a05e7a='30fbb922-2f27-47fe-be7b-d0867ba52cba'
  - entry.persona: 7fd01939='co_midmarket_manufacturing', 42c609c0='operations_leaders'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-052

- **winner_id**: `202b1b49-aa68-40d2-869e-3256ed0c330b`
- **loser_ids**: `2bdbea33-e848-4125-a21d-cb672f31339c, 6284d840-e47e-4c3f-b78d-69b6fa1268f0, f088357d-7040-4a5a-ad21-0d1edbbd226a`
- **conflict_reasons**:
  - company: 202b1b49='343ffa34-e09b-4178-94a8-fd68eef318a7', 2bdbea33='f119a9fb-01a5-4c1f-bd45-e2b09fa546d2'
  - entry.persona: 4f6126ff='operations_leaders', 404a52cc='digitalization_champions'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-053

- **winner_id**: `9861d321-0b44-4266-a5d3-5f48ac906a31`
- **loser_ids**: `22663446-702e-4421-a2b3-d363dc5244fa, 75ea8932-1eb8-4fc2-bd3b-1cca071b4dc2, a71a8abe-8fd2-4666-93e7-c5b0ff1aae2b`
- **conflict_reasons**:
  - company: 22663446='b58395ad-8ace-4660-a0a3-e38557ac687f', 9861d321='513e289f-ddfc-4bbe-b98c-71ad84fc417d'
  - entry.persona: 00c37a63='operations_leaders', a0337b62='digitalization_champions'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-054

- **winner_id**: `5b98d020-bc7e-4cc4-a7b7-ab9cbe33156e`
- **loser_ids**: `2528226c-d81c-4e30-b36c-cda59ca78715, 837c31dc-9a26-43aa-ab43-bae2e9de033e, b6fdee5d-2cb1-4489-9b35-120ebe8be61d, dbd2315b-081e-4c68-a58c-dce440599897`
- **conflict_reasons**:
  - company: 2528226c='14b2b39f-0b4c-4c32-8877-a9a5be6a8a77', 5b98d020='85862400-a8c3-439d-a155-64bdf49a7454'
  - entry.persona: 3a7d207f='operations_leaders', 4413f234='digitalization_champions'
- **list_entry_actions**: 5 total

### https://www.linkedin.com/in/prospect-055

- **winner_id**: `d1d2a96e-cc0f-4ba5-bcd1-0ab18456dea9`
- **loser_ids**: `302b6ae2-7e78-436c-b95c-9fa325795b2d, 9bf7af3e-31e2-489a-8801-683fb9a696bc, a43895a2-8746-4eec-a399-a99027c27824`
- **conflict_reasons**:
  - company: 302b6ae2='b9b8f887-5230-4d7a-ab65-f25694eb9a93', d1d2a96e='eec355ca-f29c-4043-a93e-6a7cdc5952d3'
  - entry.persona: a62c59b6='operations_leaders', 67c576e6='digitalization_champions'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-056

- **winner_id**: `8f5c8708-1fa7-45d1-8b66-8705f26f2291`
- **loser_ids**: `30be5dd5-7199-4b9f-8954-c6fd3c245342, 380dfd28-9307-4961-8437-02568fe584c2, 94a808a9-cee9-4f94-8fea-5563994370e3, f1c91f4a-46bf-41fb-b502-0d0f57238966`
- **conflict_reasons**:
  - company: 30be5dd5='4ec7077e-f6d1-4ac5-b8a8-858ae748c3d1', 8f5c8708='717ac60c-3d02-47dc-ad25-92c9d63f04b9'
  - entry.persona: 7dc990f8='executive_sponsors', 4f9bafe9='digitalization_champions'
- **list_entry_actions**: 5 total

### https://www.linkedin.com/in/prospect-057

- **winner_id**: `5b991888-e1c0-47df-87d0-bfb2a8582226`
- **loser_ids**: `4aba0d85-8674-4945-b3aa-550477101db3, 6364da7d-f0c1-41ee-a269-35174d0e58ea, c0b0cf4d-468f-4a5f-82ce-01dce210b509, ccded83a-25ed-4d9e-b8fb-db4a1391e2e4`
- **conflict_reasons**:
  - company: 4aba0d85='a6a9cbd5-4991-4c8d-ae88-f62adceeaddd', 5b991888='18054707-d886-550c-b7db-173806ab7d1c'
  - entry.persona: 77d23e1f='operations_leaders', 778bf03c='digitalization_champions'
- **list_entry_actions**: 5 total

### https://www.linkedin.com/in/prospect-058

- **winner_id**: `92f11f07-4956-47ee-a7e4-9d1a65b8867c`
- **loser_ids**: `56152151-68ee-4a2a-b131-a9b5b53bd5f5, 77a40b1b-21d2-43ab-abb1-52247cab2afc, 8115dc7c-4073-47ba-bde4-f1aa4983b6a9, aa51d76b-a987-48d1-a44c-c82a1680ecad`
- **conflict_reasons**:
  - company: 56152151='54955e37-5e10-4d50-9ae2-a430194fe627', 77a40b1b='b77a8b0a-5034-412d-9d10-6314b51ae87b'
  - entry.persona: f85f64e5='operations_leaders', 9cd1e5b6='digitalization_champions'
- **list_entry_actions**: 5 total

---

## Shape: `company` (2 groups)

### https://www.linkedin.com/in/prospect-059

- **winner_id**: `0f1e158d-6d4b-446a-ae6f-7a26d30be2b0`
- **loser_ids**: `3bf52d60-d516-4b20-b59a-9275a829be97, 6b1b3adc-fb59-447e-b39d-04e8be88857a, cd18efe7-8e9b-4344-86cb-ff9662ad2642`
- **conflict_reasons**:
  - company: 0f1e158d='5a7634f6-510e-4559-9d1b-5e825209f8dc', 3bf52d60='599aaafa-eb08-49db-a314-803ea7718d54', 6b1b3adc='30dd3f96-599c-4e7e-82e4-f82aca734c62', cd18efe7='654398a5-d18f-402c-9fa2-9b23960be09d'
- **list_entry_actions**: 4 total

### https://www.linkedin.com/in/prospect-060

- **winner_id**: `706245e3-9bcc-4bf3-a254-cb15e018a77b`
- **loser_ids**: `2d1c73b5-a5f4-4ba3-bff4-66abb2110c17, 338eed7c-c7eb-4fd7-ba7c-200a85f4ae04, 47435707-6fb3-4bc4-9500-ce33e4fc5aeb, 9907c6eb-9ae2-4134-bb2b-14e824a8ceef, bb9261d3-eb8f-4ccc-83aa-2006a07fef6a, caa87a90-5608-425a-b169-a067082a9aa5, d08d9698-fac7-42f7-8adf-c27c1a8ab81f, e218024c-0e1e-4166-8001-1ab307fe9e36`
- **conflict_reasons**:
  - company: 338eed7c='54fa12b7-71af-4814-b20a-3ca66b2dc0d5', 47435707='6f8600c3-dde2-403c-bfb0-400b5ac0ba85'
- **fields_to_merge** (copied to winner): `['company']`
- **list_entry_actions**: 6 total

---

## Shape: `email+name` (2 groups)

### https://www.linkedin.com/in/prospect-061

- **winner_id**: `3793823f-de75-5231-a49e-5034f67bf9bc`
- **loser_ids**: `afdf5025-9c56-59a9-a0e9-b722e97dab3a`
- **conflict_reasons**:
  - name: 3793823f='person 1', afdf5025='person 2'
  - email_addresses: 3793823f='user1@example.com', afdf5025='user2@example.com'

### https://www.linkedin.com/in/prospect-062

- **winner_id**: `49e889f1-c1a4-50e0-be1d-1ed9a5c420ff`
- **loser_ids**: `e16f220f-1a09-5fdf-ad91-b85f328be4f1`
- **conflict_reasons**:
  - name: 49e889f1='person 3', e16f220f='person 4'
  - email_addresses: 49e889f1='user3@example.com', e16f220f='user4@example.com'

---

## Shape: `email` (2 groups)

### https://www.linkedin.com/in/prospect-063

- **winner_id**: `3ff8acbb-df59-503e-9a96-72be78d36920`
- **loser_ids**: `f0fc9573-de3b-5786-a73e-bd57189b26a1`
- **conflict_reasons**:
  - email_addresses: 3ff8acbb='user5@example.com', f0fc9573='user6@example.com'

### https://www.linkedin.com/in/prospect-064

- **winner_id**: `ad13d1e2-fd4d-5bcf-a5ef-fe9c5c5cb8a7`
- **loser_ids**: `4552b43a-19ee-51aa-943b-44449407af4a`
- **conflict_reasons**:
  - email_addresses: 4552b43a='user7@example.com', ad13d1e2='user8@example.com'

---

## Shape: `company+email` (2 groups)

### https://www.linkedin.com/in/prospect-065

- **winner_id**: `df076ee9-c251-55cb-a6e7-c0fc7fde1818`
- **loser_ids**: `87c8b2cb-84f4-53ca-b27e-d3f176dd61e4`
- **conflict_reasons**:
  - company: 87c8b2cb='3af5220d-5f8c-5220-a239-641e6360c80a', df076ee9='d893489c-c6e9-5ce2-b4da-815e1aafee65'
  - email_addresses: 87c8b2cb='user9@example.com', df076ee9='user10@example.com'

### https://www.linkedin.com/in/prospect-066

- **winner_id**: `956cfe46-7360-56a0-9aa3-d42276102cb7`
- **loser_ids**: `d585e5da-0bf9-58bf-b040-18b20d4d593a`
- **conflict_reasons**:
  - company: 956cfe46='da63d4dd-b3a8-5eb2-aa94-b1c617f000ca', d585e5da='0483e089-c092-59cd-9629-f7da1407ce4a'
  - email_addresses: 956cfe46='user11@example.com', d585e5da='user12@example.com'
- **fields_to_merge** (copied to winner): `['job_title']`

---

## Shape: `company+email+name` (1 groups)

### https://www.linkedin.com/in/prospect-067

- **winner_id**: `00e5e3aa-549b-52d6-9266-5a06ef9e2840`
- **loser_ids**: `426cf199-c7cc-5c37-b984-15c7f486518e`
- **conflict_reasons**:
  - name: 00e5e3aa='person 5', 426cf199='person 6'
  - company: 00e5e3aa='5f41a35a-8caf-5172-a55f-8542d1cf2196', 426cf199='c341f487-475c-5733-b3da-23fc3046c0cc'
  - email_addresses: 00e5e3aa='user13@example.com', 426cf199='user14@example.com'

---

## Shape: `company+entry.persona+name` (1 groups)

### https://www.linkedin.com/in/prospect-068

- **winner_id**: `b7444de1-85ab-5c55-a189-99b6eb23e057`
- **loser_ids**: `0d75e2eb-e4be-47b9-bab2-0f5bf129356e, 7d37733e-7b37-4b64-95a9-16a036dcfebc, 9847bc0c-a81e-4102-b8ad-8c7b36bed807, cfac6dd2-499c-415d-a5e0-93527c5e2320, dc4a2c79-e24f-4712-a571-a601667edda2`
- **conflict_reasons**:
  - name: 0d75e2eb='person 7', b7444de1='person 8'
  - company: 0d75e2eb='6ab7e4ba-5055-4508-9532-af66916e5b97', 7d37733e='18430ce8-f42a-453a-b3e0-0882a67a6e5e'
  - entry.persona: 4e6dda10='operations_leaders', be76b4a1='digitalization_champions'
- **fields_to_merge** (copied to winner): `['job_title']`
- **list_entry_actions**: 5 total

---

## How to apply your decisions

Open `dedup_report.json`. For each conflict group above:

- **To merge it**: move the entire group object from `people.conflict_groups` into `people.approved_conflict_groups`
- **To leave it alone**: move the group into `people.skipped_groups`

Every group must be in one of those two lists — `apply` refuses to run if any conflict group is still in `conflict_groups`.

Then:

```bash
cd <repo-root>
git checkout feat/attio-dedup       # PR #4
python3 scripts/attio_dedup.py apply --report dedup_report.json --pretend   # dry run first
# review dedup_apply_log.jsonl
python3 scripts/attio_dedup.py apply --report dedup_report.json             # live
```