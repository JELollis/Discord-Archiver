## ArchiveBot Admin Instructions

> ⚠️ **Deletion is permanent.** Carefully review the confirmation message before clicking **Confirm Delete**. The confirmation expires after 60 seconds and only the admin who ran the command can approve it.

### Delete the oldest category only

To delete the `Summer 2023 Archive` category and **every channel inside it**:

`/delete target_type:Category targets:Summer 2023 Archive`

1. Verify the confirmation lists exactly **one category** and the expected number of channels.
2. Click **Confirm Delete**.

Do not select `Both` or enter any additional category names.

### Delete only the channels shown in the screenshot

This leaves their parent category in place and deletes only the 12 named channels:

`/delete target_type:Channel targets:cpt-168-summer-2023, cpt-209-summer-2023, cpt-230-summer-2023, cpt-267-summer-2023, ist-110-summer-2023, ist-190-summer-2023, ist-201-summer-2023, ist-226-summer-2023, ist-266-summer-2023, ist-272-summer-2023, ist-292-summer-2023, spc-205-summer-2023`

Verify the confirmation reports **0 categories and 12 channels**, then click **Confirm Delete**.

## Populate Spring 2027 Channels

The corresponding course roles—such as `CPT-113`—must already exist. If the `Lab Tech` role is missing, the channels will still be created, but without Lab Tech access.

- **CPT**

  `/populate category:CPT term:spring year:2027 courses:113, 127, 170, 189, 209, 227, 230, 231, 234, 236, 237, 239, 257, 264, 267, 270, 273, 275, 280, 283, 289`

- **CYB**

  `/populate category:CYB term:spring year:2027 courses:110, 201, 269, 282, 293, 294`

- **IST**

  `/populate category:IST term:spring year:2027 courses:190, 191, 198, 201, 202, 203, 220, 226, 239, 257, 258, 266, 267, 272, 278`

- **SPC**

  `/populate category:SPC term:spring year:2027 courses:205, 208, 209`

- **SOC**

  `/populate category:SOC term:spring year:2027 courses:101`

- **HSS**

  `/populate category:HSS term:spring year:2027 courses:105`

- **HIS**

  `/populate category:HIS term:spring year:2027 courses:122`
