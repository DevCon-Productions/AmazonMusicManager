import asyncio
from browser import BrowserManager
from auth import ensure_logged_in
from config import BASE_URL, LONG_TIMEOUT
from utils import setup_logging

async def main():
    setup_logging(verbose=True)
    async with BrowserManager() as bm:
        await ensure_logged_in(bm.page)
        page = bm.page

        # Navigate to playlists page
        await page.goto(f"{BASE_URL}/my/playlists/all", wait_until="domcontentloaded", timeout=LONG_TIMEOUT)
        await page.wait_for_timeout(5000)

        # Click "Create Playlist"
        print("Step 1: Clicking Create Playlist...")
        await page.evaluate("""() => {
            const els = document.querySelectorAll('music-button, button, a, [role="button"]');
            for (const el of els) {
                const text = (el.textContent || '').trim().toLowerCase();
                if (text.includes('create playlist')) {
                    el.click();
                    return true;
                }
            }
            return false;
        }""")
        await page.wait_for_timeout(3000)

        # Use Playwright native click on "Curate a playlist" row
        print("Step 2: Native-clicking 'Curate a playlist'...")
        curate_handle = await page.evaluate_handle("""() => {
            const d = document.querySelector('[role="dialog"], [aria-modal="true"]');
            if (!d) return null;
            const rows = d.querySelectorAll('music-image-row');
            for (const row of rows) {
                const pt = (row.getAttribute('primary-text') || '').toLowerCase();
                if (pt.includes('curate')) return row;
            }
            return null;
        }""")

        is_null = await curate_handle.evaluate("el => el === null")
        if is_null:
            print("  Could not find Curate row!")
            input("Press Enter to close...")
            return

        # Use Playwright's real click
        box = await curate_handle.bounding_box()
        if box:
            await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            print(f"  Clicked at ({box['x'] + box['width']/2:.0f}, {box['y'] + box['height']/2:.0f})")
        else:
            print("  No bounding box!")

        await page.wait_for_timeout(5000)

        # Dump the new state
        print("\nStep 3: After native click on Curate...")
        state = await page.evaluate("""() => {
            const result = {
                url: window.location.href,
                inputs: [],
                contentEditables: [],
                dialogs: [],
                focusedTag: document.activeElement?.tagName?.toLowerCase() || '',
                focusedType: document.activeElement?.type || '',
                focusedId: document.activeElement?.id || '',
            };

            document.querySelectorAll('input, textarea').forEach(inp => {
                result.inputs.push({
                    type: inp.type || 'textarea',
                    id: inp.id,
                    placeholder: inp.placeholder,
                    value: inp.value,
                    visible: inp.offsetParent !== null,
                });
            });

            document.querySelectorAll('[contenteditable="true"]').forEach(el => {
                result.contentEditables.push({
                    tag: el.tagName.toLowerCase(),
                    text: el.textContent?.substring(0, 50),
                    className: (el.className || '').substring(0, 60),
                });
            });

            document.querySelectorAll('[role="dialog"], [aria-modal="true"]').forEach(d => {
                const children = [];
                d.querySelectorAll('*').forEach(el => {
                    const tag = el.tagName.toLowerCase();
                    const text = (el.textContent || '').trim();
                    if (text || el.type || el.placeholder || el.getAttribute('contenteditable')) {
                        children.push({
                            tag: tag,
                            text: text.substring(0, 60),
                            type: el.type || '',
                            placeholder: el.placeholder || '',
                            contentEditable: el.getAttribute('contenteditable') || '',
                            visible: el.offsetParent !== null,
                        });
                    }
                });
                result.dialogs.push({
                    id: d.id,
                    childCount: children.length,
                    children: children.slice(0, 30),
                });
            });

            // Check page header
            const header = document.querySelector('music-detail-header');
            if (header) {
                result.headerHeadline = header.getAttribute('headline') || '';
            }
            const h1 = document.querySelector('h1');
            if (h1) result.h1Text = h1.textContent?.trim().substring(0, 50);

            return result;
        }""")

        print(f"  URL: {state['url']}")
        print(f"  Focused: <{state['focusedTag']}> type={state['focusedType']} id={state['focusedId']}")
        if state.get('h1Text'):
            print(f"  H1: '{state['h1Text']}'")
        if state.get('headerHeadline'):
            print(f"  Header headline: '{state['headerHeadline']}'")

        print(f"\n  Inputs ({len(state['inputs'])}):")
        for inp in state['inputs']:
            print(f"    type={inp['type']} id='{inp['id']}' placeholder='{inp['placeholder']}' value='{inp['value']}' visible={inp['visible']}")

        print(f"\n  Content-editable ({len(state['contentEditables'])}):")
        for ce in state['contentEditables']:
            print(f"    <{ce['tag']}> text='{ce['text']}' class='{ce['className']}'")

        print(f"\n  Dialogs ({len(state['dialogs'])}):")
        for d in state['dialogs']:
            print(f"    id='{d['id']}' children={d['childCount']}")
            for c in d['children']:
                vis = 'V' if c['visible'] else 'H'
                ce = f" CE" if c['contentEditable'] else ''
                print(f"      [{vis}] <{c['tag']}> '{c['text'][:50]}' type='{c['type']}' ph='{c['placeholder']}'{ce}")

        input("\nPress Enter to close browser...")

asyncio.run(main())
