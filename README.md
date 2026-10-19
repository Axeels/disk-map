# Disk Map

Windows fills up and you have no idea where the space went. Downloads, AppData, old installers, game leftovers, Cursor databases, temp folders that never get cleaned. Disk Map scans a drive or folder and shows you the big pieces as a map you can click through.

It is a small local app. About 150 KB of Python and a browser UI. No installers, no cloud account, no packages to pip install. Double click `Start Disk Map.bat`, a page opens on your machine, and that is it. Keep the console window open while you use it. Close the window when you are done.

## How it works

Pick a drive from the home screen, or jump straight into common hiding spots like Downloads, AppData, Temp, Pictures, or Windows Temp. You can also paste any folder path or browse for one.

While it scans you see how much it has counted so far. A full drive can take a few minutes. A single folder is usually quick. You can stop early and keep whatever it already found.

When the scan finishes you get three views.

**Map.** Big blocks take more space. Double click a folder block to open it. Click a file to select it. The list under the map shows the same items with sizes and how much of the folder they own.

**Files.** Search by name. Filter by size, type, or how long something has been sitting there. Export the matching list to CSV if you want it in Excel.

**Insights.** Biggest folders at the top of the scan, file type breakdown, age of the data, caches and temp folders that stood out, large files that have not been touched in months, pagefile and disk images, empty folders, and a duplicate finder for copies that match in size and contents.

Folder totals always include every file. The lists lean toward the larger ones so you are not staring at thousands of tiny files first. Open any folder and hit **Show every file here** when you need the small stuff too.

## Removing things

Select one or more items and send them to the Recycle Bin. That is the normal path, so you can restore them if you change your mind. Permanent delete exists, but you have to type `DELETE` first.

Disk Map will not wipe a whole drive, and it will not delete `Windows`, `System32`, or `WinSxS`. You also get warnings when something sits under Program Files or similar places that can break an install.

From the same panel you can reveal a file in Explorer, open it, or copy the path.

## Requirements

Windows, plus [Python 3](https://www.python.org/downloads/). That is all. The page only talks to `127.0.0.1` on your PC.

If you prefer the command line:

```bat
py -3 diskspace.py
```

```bat
python diskspace.py
```

## Why this exists

Windows Storage settings tell you broad categories. Search only helps if you already know the filename. Disk Map is for the middle ground: open a drive, see the weird 30 GB file buried in AppData, and deal with it without guessing.
