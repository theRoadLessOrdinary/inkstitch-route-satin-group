# Route Satin Group

An Inkscape extension for [Ink/Stitch](https://inkstitch.org) satin columns. It sets the stitch direction and order of the satin columns in a group, and adds travel paths and trims, so that the needle never jumps outside the design or across work that is already stitched.

## What it does

Select one group of satin columns and run **Extensions > Map's Edge Tools > Embroidery > Satin > Route Satin Group**.

**Layers.** Each group directly inside the selected group is one layer of stitching, for example "phase 1", "phase 2" and "phase 3". The layers are sewn in their order in the document, and each one is routed on its own: columns are never moved from one layer to another. A group with no groups inside it is a single layer.

**Direction.** Ink/Stitch sews a satin column from one end of its rails to the other. For every column, the tool points both rails the same way, so the column finishes at the end nearest the start of the next column. Directions are worked out from the last column back to the first.

**Moves between columns.** The columns together cover one area, the outline of the whole design. Every move from the end of one column to the start of the next must:

- stay inside that area, and
- not cross anything that is already stitched.

When a straight jump can't do that, the tool uses the first of these that works:

1. **A travel path.** A running stitch path from the end of the column, over area that is not stitched yet (later columns cover it), to a point where a straight jump can finish the move. It is placed right after the column in the same group and named "Travel after ...". It uses the column's thread color and Ink/Stitch's default running stitch.
2. **A different order.** Columns inside the layer are reordered, and their directions with them. A new order is kept only when it saves trims.
3. **A trim.** Ink/Stitch's "Trim after" is set on the column.

Trims cost machine time, so the tool keeps them to as few as it can find. Each layer sits on top of the ones before it and has less free space, so higher layers usually need more trims.

## Running it again

Every run starts clean. It clears every trim in the selected group and removes the travel paths it made before, then works everything out again. So after you add, remove or change columns, run it again.

During one Inkscape session, the tool remembers its last result for each group. When you run it again, it keeps that result up to the first column that was added, removed or changed, and works out only the rest. A change to any column also changes the outline of the design, so moves before that point are checked again too:

- a jump is kept only if it still fits inside the outline;
- a travel path or trim is kept only if the same move would still be chosen.

With nothing changed, a run reuses the whole result and says so.

## Selecting

Any of these works:

- the parent group alone;
- the parent group together with the groups inside it (for example a range selected in the Objects panel);
- several groups that sit side by side in one group, each taken as a layer.

Only paths that Ink/Stitch treats as satin columns are routed. Other objects in the group are left where they are.

## Messages

After a run, Inkscape shows what changed:

- how many columns were reversed;
- how many had rails pointing opposite ways and now match;
- how many moved to a new place in the order;
- the travel paths added, by id;
- the columns that now have a trim after them.

## Settings in the code

These are constants near the top of `route_satin_group.py`, in millimeters:

| Constant | Default | Meaning |
| --- | --- | --- |
| `GAP_MM` | 0.25 | A gap between columns narrower than twice this counts as inside the outline. |
| `END_CLEAR_MM` | 0.5 | A move may touch stitched area this close to where it leaves or lands. |
| `TRAVEL_CLEAR_MM` | 0.2 | A travel path keeps this far inside the outline's edge. |
| `TRAVEL_MARK_MM` | 0.15 | Half the width a stitched travel path takes up, for the moves after it. |
| `GRID_MM` | 0.25 | Cell size of the travel path search. |

## Requirements

- Inkscape 1.4 or later.
- Ink/Stitch, to stitch the result.
- The Python modules `numpy` and `shapely` (2.0 or later) in the Python that Inkscape runs extensions with. The Inkscape 1.4 Flatpak includes both.

## Install

Copy this folder into your Inkscape user extensions folder and restart Inkscape. To find that folder, open **Edit > Preferences > System** and look at **User extensions**. For the Flatpak version on Linux it is `~/.var/app/org.inkscape.Inkscape/config/inkscape/extensions/`.

## Known limits

- The first run on a large group can take a minute.
- The column outline comes from the two rails of each column, sampled every 0.2 mm. Rails are taken to be the two longest subpaths of the path.
- The reorder search moves one column at a time, so it can miss an order that needs several columns to move together.
- The direction of the very last column is left as it is.

## License

GPL-3.0. See [LICENSE](LICENSE).
