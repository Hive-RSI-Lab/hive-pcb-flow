## 2026-11-16 [datasheet_extract][render][drawing] A word that finds drawings also finds most of a long datasheet: rank by the phrases that name the drawing
Found writing the part records for haos-voice's capacitors and logic (Murata GRM155R71C104KA88D, TI SN74LVC1G07DBVR), 2026-11-15 (#1333).
datasheet_extract --render drew every page holding any word of
DRAWING_KEYWORDS. "mounting", "dimension", "drawing", "hole" and "panel"
are on most pages of a catalogue or of a datasheet that covers a family:
it drew 25 of the 33 pages of the GRM155 catalogue, whose land is Table 2
on page 27, and 24 of the 49 pages of the SN74LVC1G07, which has a land
pattern for each of its packages. Over the board's 50 datasheets the rule
drew 335 pages. The agent writing a record then opened the PNGs one by one
or pruned the folder by hand, and paid for each as a 200 dpi image.

The fix ranks the words. A page that names the land pattern or the package
drawing (LAND_PHRASES: "land pattern", "package outline", "example board
layout", ...) renders; a page with only a general word renders only when no
page has a phrase. --part keeps the drawing pages that name an MPN or a
package code (DBV0005A), and drops none when no page names it. Every
candidate page is listed in rendered.pages with its words and whether it
rendered, and rendered.note says why, so a page left out is still one the
agent can open. The same 50 datasheets now draw 160 pages. When a word list
gates costly work, weigh each word by how much it says: a word found on
most pages is evidence only when nothing more specific is there.

Triage: now L1 | target L1 | owner scripts/datasheet_extract.py | status done | note a drawing page with no phrase still waits on the agent reading rendered.pages; TI's EXAMPLE STENCIL DESIGN pages hold no drawing word and render under neither rule
