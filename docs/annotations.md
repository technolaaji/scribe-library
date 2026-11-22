# Exporting highlights and handwritten notes, with their position in the book

Research notes from a Kindle Scribe read over USB (MTP), November 2026.
Everything here was checked against a real device and against the Kindle's own
`My Clippings.txt`. The working implementation is `engine/annotations.py`.

**Short version.** For any book whose file is on the device as KFX (purchased
books in KFX and Send-to-Kindle / sideloaded books), you can export every
highlight with its exact text and Kindle location, and every handwritten note
as an image anchored to the location it was written at. All of it is offline
over USB. Two gaps: books delivered by Amazon whose annotation index the device
doesn't expose over USB, and books you've deleted from the device (their text
is gone). Both are covered below.

---

## 1. Where things live on the device

Paths are as MTP shows them (the Kindle's user storage root).

| What | Path | Format |
| --- | --- | --- |
| Book | `documents/<Title - Author>.kfx` (or `.azw3`, `.pdf`, `.epub`…) | KFX container |
| Per-book annotation index | `documents/<Title - Author>.sdr/<Title - Author><hash>.yjr` | KRDS |
| Per-book reading state | `documents/<Title - Author>.sdr/<Title - Author><hash>.yjf` | KRDS |
| Handwriting written *in* a book | `.notebooks/<ASIN>!!EBOK!!notebook/nbk` (store books)<br>`.notebooks/<id>!!PDOC!!notebook/nbk` (personal docs) | KDF (SQLite + Ion) |
| Standalone notebooks | `.notebooks/<UUID>/nbk` + `.notebooks/thumbnails/<UUID>.png` | KDF |
| Plain-text clippings | `documents/My Clippings.txt` | text |
| Cloud sync queue | `.sync/KSDKNoteSyncDB.sqlite` | SQLite |

Things worth knowing:

- `<hash>` in the `.yjr` name is the same 32-hex string for every book on one
  device (here `ac0382d3…f796`). It identifies the device or account, not the
  book.
- A `.yjr.bad_file` next to a `.yjr` is an older copy the reader set aside.
  Use the newest `.yjr`.
- Folders named `<Title>_<ASIN>.sdr` (an ASIN or 32-character id after the last
  `_`) were empty over MTP on this device. There were 53 of them. These are
  books delivered by Amazon; their annotation index sits in internal storage
  that USB doesn't expose. See §6.
- `KSDKNoteSyncDB.sqlite` only holds *pending* cloud actions (here 614 rows
  for 9 notebooks). It has no titles and no anchors, so it's not a useful
  source.

## 2. The `.yjr` file (KRDS)

KRDS ("Kindle Reader Data Store") is a small typed binary format:

```
magic   00 00 00 00 00 1A B1 26
values  <type byte> <payload> …
```

| Type byte | Value |
| --- | --- |
| `00` | boolean (1 byte) |
| `01` | int32, big-endian |
| `02` | int64, big-endian |
| `03` | string: 1 flag byte (`01` = null) then uint16 BE length, then UTF-8 |
| `04` / `05` | float32 / float64 |
| `07` / `08` | int16 / byte |
| `FE` | start of a named object: name (string, no type byte), then values until… |
| `FF` | …end of object |

The file starts with `long 1` (version) and `int N` (number of top-level
objects), then objects such as `font.prefs`, `next.in.series.info.data`,
`annotation.cache.object` and `ReaderMetrics`.

`annotation.cache.object` holds the annotations:

```
annotation.cache.object
  int <number of annotation types>
  int <type id>  saved.avl.interval.tree { int <count>, <annotation>… }
  int <type id>  saved.avl.interval.tree { … }
```

| Type id | Object name | Fields after the common five |
| --- | --- | --- |
| 0 | `annotation.personal.bookmark` | none |
| 1 | `annotation.personal.highlight` | none |
| 2 | `annotation.personal.note` | note text |
| 10 | `annotation.personal.handwritten_note` | **canvas id** |

Every annotation starts with the same five values:

1. start position (string)
2. end position (string)
3. created, ms since epoch (int64)
4. last modified, ms since epoch (int64)
5. template (string, `0￼0` in every sample)

A real handwritten-note record:

```
annotation.personal.handwritten_note:
  str  'Aa8IAAAAAAAA:139451'      start
  str  'Aa8IAAAAAAAA:139451'      end (same: a point anchor)
  long 1781433655936              created
  long 1781433660007              modified
  str  '0￼0'
  str  'cZdPFUsCOQU_FGr2IPT0S_A0'   → canvas in the in-book notebook
```

## 3. Positions

A position string looks like `AZUIAAAAAAAA:110879`.

- **Before the colon:** base64 of 9 bytes:
  `version (1 byte, = 1) · eid (uint32 LE) · offset in that element (uint32 LE)`.
  `AZUIAAAAAAAA` → `01 95 08 00 00 00 00 00 00` → eid 2197, offset 0.
- **After the colon:** the absolute KFX *position id* (pid) for that point.

KFX books are built from content elements with ids (eids). kfxlib's
`collect_content_position_info()` lists every element as
`(pid, eid, eid_offset, length, text)`. Resolving a position is a lookup:

```
pid = chunk.pid + (offset - chunk.eid_offset)   for the chunk with that eid covering offset
```

The computed pid matched the number after the colon for all 168 start/end
positions tested (84 annotations across 3 books), which confirms the decoding. The highlighted text is then the text of
the chunks between the start and end pid.

**Location numbers.** The "Location N" the Kindle shows comes from the book's
location map (`$550`), via kfxlib's `collect_location_map_info()`. Location is
1 + the number of location entries with `pid ≤` the position. This matched
`My Clippings.txt` exactly: pid 68758 → Location 654, where clippings say
*"Your Highlight on Location 654-701"*. If a book has no location map, kfxlib
can approximate one (`generate_approximate_locations`, 110 positions per
location). Don't use `pid / 150`: it's off by hundreds.

**Pages.** KFX books with real page numbers carry a page list in their
navigation (`$237`). Positions map to pages the same way, through that list.
Not implemented yet; locations are what the Kindle and Clippings use.

## 4. Handwritten notes in books

Writing in a book (handwritten sticky notes and margin notes) doesn't go into
the book's `.sdr`. It goes into a separate KDF notebook keyed by the book's id:

- store books: `.notebooks/<ASIN>!!EBOK!!notebook/nbk`
- personal documents (Send to Kindle, Calibre, sideloaded):
  `.notebooks/<id>!!PDOC!!notebook/nbk`, where `<id>` is the book's ASIN-style
  metadata id (here `K9JON6825QIL49CJIBVTFA0IS823CRP6`, which kfxlib reports
  as both `ASIN` and `content_id` of the `.kfx`)

The notebook's `kindle_title_metadata` also records the book's `book_id` (for
example `NQkJ1KkRQca1hG9egu-QAA0`), which matches the `book_id` in the KFX
metadata. That gives a second way to link the two.

Inside, each note is one **canvas**, a `$260` section with
`nmdl.canvas_width/height` (2520 ppi), holding `nmdl.stroke` entries (points,
pressure, tilt, thickness, brush, colour). Unlike a standalone notebook, these
canvases are *not* in the notebook's reading order. kfxlib flags them as
"unreferenced pages" and drops them when it rebuilds the book. To render them:

1. `YJ_Book(dir).decode_book(pure=True)` (keeps unreferenced sections),
2. put every `$260` section into the reading orders (`$538`, `$258`),
3. `convert_to_epub()` → one SVG per canvas, named by canvas id,
4. rasterise the SVG (cairosvg).

The canvas id in the `.yjr` record (`cZdPFUsCOQU_FGr2IPT0S_A0`) is the SVG's
name, so each image lands at its anchor's location.

Canvas sizes seen: `15624 × 20832` (full page), `13734 × 7350` (wide strip),
`5208 × 1672` (small, placed `float: right; width: 25%`, a margin note).

**Position data lives only in the `.yjr`.** The canvases themselves carry no
anchor. A canvas with no `.yjr` record pointing at it can still be exported,
just without a location.

### Active Canvas notes have no anchor over USB

Two kinds of handwriting go into a book:

- **Handwritten sticky notes** (a full-page canvas, `15624 × 20832`): the
  `.yjr` gets an `annotation.personal.handwritten_note` record pointing at the
  canvas, as above.
- **Active Canvas** (writing in a space opened inside the text, a
  `13734 × 7350` canvas): the canvas *is* in the in-book notebook's reading
  order, but **no record is written anywhere USB can see**.

This was checked directly. A note was written in *Plain Life* (Antonia Pont)
and the whole USB file tree was diffed before and after. Only these changed:
the in-book notebook (`nbk`, `nbk-journal`, `actions.log`) and the book's
`.yjr`/`.yjf`. The `.yjr` was rewritten with an empty `annotation.cache.object`.
The notebook's `actions.log` (base64 Ion records: `CreateNoteAction`,
`InsertStrokeAction`…) gives the note a position of `-1`, a position inside the
notebook, not the book. `.sync/KSDKNoteSyncDB.sqlite` holds no reference to
the canvas. So the reader keeps the anchor in internal storage.

**Recovering it.** The `.yjf` reading-state file has an `lpr` (last position
read) object: a position string plus the time it was saved, in ms. Seven
seconds after the note was saved, `lpr` was set to `AbIDAAAAAAAA:180432`,
which is Location 1487, where the note was written. The engine uses this:
when a book has exactly one unplaced canvas and `lpr` was saved within 15
minutes of the in-book notebook changing, that position is recorded in the
registry (`note_positions`) as the note's approximate anchor and kept from
then on. The UI marks it "≈ Loc N · where you were reading when you wrote it".

Limits: the position has to be picked up by a sync soon after writing,
before you read on; and it only works when one note is unplaced (with several
unknown canvases there's no way to tell which one is new). Tracking the last
`$10` canvas id in `actions.log` would remove the second limit; that file isn't
synced yet.

## 5. The full chain

```
documents/X.kfx ──(kfxlib metadata: ASIN / content_id)──┐
     │                                                  ▼
documents/X.sdr/X<hash>.yjr            .notebooks/<ASIN>!!PDOC!!notebook/nbk
  highlight  start,end ──► kfxlib positions ──► text + Location
  handwritten_note start, canvas_id ─────────────────► canvas → strokes → PNG
```

On this device, for books still on it:

| Book | Highlights (text resolved) | Handwritten notes |
| --- | --- | --- |
| How to Love Better (Yung Pueblo) | 26 / 26 | 1 at Location 1262, 2 without position |
| The Minimalist Entrepreneur | 48 / 48 | 3 without position |
| The Practice of Not Thinking | 9 / 9 | — |
| Stoner (John Williams) | — | 2 without position |
| Plain Life (Antonia Pont) | — | 1 Active Canvas note at ≈ Location 1487 (from `lpr`) |

Running the export on a book takes about 0.6 s. Results are cached until the
book, its `.yjr` or the in-book notebooks change.

## 6. Gaps, and what to do about them

**Books delivered by Amazon (ASIN-suffixed `.sdr`, empty over USB).** Their
highlights and handwriting anchors are in the reader's internal annotation
store, not on the USB-visible partition. Over USB you get:

- the handwriting canvases (via `.notebooks/<ASIN>!!EBOK!!notebook`), without
  position,
- highlight *text* from `My Clippings.txt` (it has locations but no handwriting).

To get positions for those books, use one of:

- **Kindle "Export notes"** (open the book's Notebook on the device or in the
  Kindle app → Export), which emails a file with highlights and locations.
  Whether handwritten notes are included depends on firmware; not verified
  here.
- **read.amazon.com/notebook** in a browser, which lists highlights and typed
  notes by location for store books.
- A jailbroken device can read the internal store directly. Out of scope here.

**Books no longer on the device.** Their `.sdr` survives (3 such books here,
109 annotations), but with no book file there's no text or location map.
`My Clippings.txt` can fill in the text: each clipping's *"Added on …"* time
equals the record's `created` timestamp to the second (device local time).
That's a reliable join key. Not implemented yet.

**PDFs.** PDF annotations use page/coordinate positions and a different
sidecar layout. `engine/pdf_annotations.py` is an older, experimental overlay
for those. Not covered by this export.

**DRM.** Text resolution needs a book kfxlib can open. DRM-protected KFX books
fail at `decode_book()`. The annotations are still listed, with raw positions
and no text.

## 7. From position to page in the reader

The app shows books as a PDF it typesets itself, so a Kindle position has no
fixed page. `locate_pages()` searches the PDF's page text for the first ten
words of each annotation's passage (normalised to letters and digits). When
the words appear more than once, it picks the match nearest the annotation's
relative position in the book (`pid / total positions`). With no text match,
it estimates the page from that relative position and marks it "≈". On the two
books tested, every annotation with a position matched exactly.

The same words are then found in the page's word boxes (`get_text("words")`)
to get the spot on the page: `x`/`y` of the passage's first word, the page's
text margin, and, for highlights, the end of the last word, possibly a few
pages later. Every word box between a highlight's first and last word is
merged into one rectangle per line, so the reader can shade the passage the
way the Kindle does. A handwritten note whose position falls inside a
highlight's start–end range is linked to it; its branch is drawn beside the
first highlighted line on that page. (In *How to Love Better* the Location
1262 note sits inside the highlight at Locations 1254–1268.)

## 8. Using it

In the app: open a book. The side panel lists its highlights and handwritten
notes in reading order, and **Export .md** downloads them as Markdown.

From the command line:

```bash
# everything in one .yjr, as JSON lines
python3 engine/annotations.py dump "library/books/sideloaded/X.sdr/X<hash>.yjr"

# Markdown + PNGs for one book (finds the .yjr and in-book notebook itself)
python3 engine/annotations.py export "library/books/sideloaded/X.kfx" --out export/
```

HTTP (engine):

- `GET /api/book/<id>/annotations` gives JSON (type, text, location, created,
  canvas image URL, warnings)
- `GET /api/book/<id>/annotations.md` gives Markdown
- `GET /handwriting/<id>/<canvas>.png` gives the rendered note

Sync pulls everything needed: `.sdr` folders come down with books, and
`.notebooks/*!!notebook` databases go to `library/notebooks/in-book/`.
