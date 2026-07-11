# Manual Dialer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the dashboard's mock Leads Directory with a manual dialer — type a number, press Call/Enter, talk — reusing the existing outbound-call flow end-to-end.

**Architecture:** Frontend-only change (spec: `docs/superpowers/specs/2026-07-11-manual-dialer-design.md`). The left glass card's contents swap from a rendered `LEADS` list to an input + Call button; a new `normalizeDialNumber()` maps typed input to E.164 (+91 default for bare 10-digit numbers) and hands off to the untouched `startOutboundCall(phone, name)`. **Spec deviation, deliberate:** the new Call button keeps the `.call-action-btn` class instead of a new style — that keeps the existing CSS block alive and makes the during-call disable logic (`querySelectorAll('.call-action-btn')` at `app.js:511`/`527`) work with zero changes, so those two spec-listed edits (CSS-block removal, disable-logic retarget) drop out as unnecessary.

**Tech Stack:** Vanilla ES6 + HTML5 + CSS (no framework, no test harness — repo convention is manual browser verification).

## Global Constraints

- Zero backend changes — `/api/call/outbound` and `startOutboundCall()` are consumed as-is (`agentGender: 'male'` unchanged).
- Number normalization rules (from spec, verbatim): strip spaces/dashes/dots/parens; `+` + 8–15 digits → as-is; exactly 10 digits → prepend `+91`; 12 digits starting `91` → prepend `+`; else invalid (inline error, no network call).
- The dialed number doubles as the display name in the active-call console (`startOutboundCall(number, number)`).
- Browser caches `app.js` — verification requires a hard refresh (known gotcha, `.agents/context/subsystem-notes.md`).

---

### Task 1: Swap the Leads Directory for the manual dialer

**Files:**
- Modify: `frontend/index.html:78-85` (left card contents)
- Modify: `frontend/app.js:16-58` (remove LEADS/renderLeads), `frontend/app.js:24-28` (init), `frontend/app.js:60-74` (setupEventListeners), new functions after `startOutboundCall`
- Modify: `frontend/style.css:246-299` (remove dead list styles, add dialer styles)

**Interfaces:**
- Consumes: `startOutboundCall(phone: string, name: string)` (`frontend/app.js:272`, unchanged).
- Produces: `normalizeDialNumber(raw: string) -> string | null` and `dialManualNumber() -> void`; DOM ids `dial-input`, `btn-dial`, `dial-error`.

- [ ] **Step 1: Replace the left card markup in `index.html`**

Lines 78-85 currently read:
```html
            <!-- Left Side: Lead List Card -->
            <div class="glass-card lead-card">
                <h2>Leads Directory</h2>
                <p class="description">Select a lead to initiate an outbound call with active voice conversion.</p>
                <div class="lead-list" id="lead-list-container">
                    <!-- Dynamic Leads will be loaded here -->
                </div>
            </div>
```
Replace with:
```html
            <!-- Left Side: Manual Dialer Card -->
            <div class="glass-card lead-card">
                <h2>Dialer</h2>
                <p class="description">Type a number and call with active voice conversion. 10-digit numbers dial as +91; use +country format for anything else.</p>
                <div class="dialer-row">
                    <input type="tel" id="dial-input" placeholder="+91XXXXXXXXXX or 10-digit number" autocomplete="off" />
                    <button id="btn-dial" class="call-action-btn">📞 Call</button>
                </div>
                <div id="dial-error" class="dial-error" style="display: none;"></div>
            </div>
```

- [ ] **Step 2: Remove the mock leads from `app.js`**

Delete lines 16-22 (the `// Mock Leads Array for MVP` comment and `const LEADS = [...]` block) and the whole `renderLeads()` function (lines 30-58, from `// Render the Leads Directory List` through its closing `}`). In the `DOMContentLoaded` handler (lines 24-28), delete the `renderLeads();` line:
```js
document.addEventListener('DOMContentLoaded', () => {
    setupEventListeners();
    initWebSocket();
});
```

- [ ] **Step 3: Add the dialer functions and wiring**

Insert directly after the closing `}` of `startOutboundCall` (currently line 297):
```js
// Normalize manual dial input to E.164 (+91 default country code).
// Returns null when the input can't be made into a plausible number.
function normalizeDialNumber(raw) {
    const cleaned = (raw || '').replace(/[\s\-\.\(\)]/g, '');
    if (/^\+\d{8,15}$/.test(cleaned)) return cleaned;
    if (/^\d{10}$/.test(cleaned)) return '+91' + cleaned;
    if (/^91\d{10}$/.test(cleaned)) return '+' + cleaned;
    return null;
}

function dialManualNumber() {
    const input = document.getElementById('dial-input');
    const errorEl = document.getElementById('dial-error');
    const number = normalizeDialNumber(input.value);
    if (!number) {
        errorEl.innerText = 'Enter a 10-digit number or full +country format';
        errorEl.style.display = 'block';
        return;
    }
    errorEl.style.display = 'none';
    startOutboundCall(number, number);
}
```
In `setupEventListeners()` (after the `btn-warmup` line), add:
```js
    // Manual dialer
    document.getElementById('btn-dial').addEventListener('click', dialManualNumber);
    document.getElementById('dial-input').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') dialManualNumber();
    });
```
No changes to `setupCallUI`/`resetCallUI`: `#btn-dial` carries the `.call-action-btn` class, so the existing `querySelectorAll('.call-action-btn')` disable/enable at lines 511/527 covers it automatically.

- [ ] **Step 4: Swap the dead list CSS for dialer styles in `style.css`**

Delete these now-unreferenced blocks (lines 246-299): `.lead-list`, the three `.lead-list::-webkit-scrollbar*` rules, `.lead-item`, `.lead-item:hover`, `.lead-info`, `.lead-name-text`, `.lead-company-text`. Keep `.lead-card` (line 234, still the card shell), `.description`, and `.call-action-btn` (used by the new button). In their place add:
```css
/* Manual Dialer */
.dialer-row {
    display: flex;
    gap: 12px;
}

#dial-input {
    flex: 1;
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 10px;
    padding: 12px 16px;
    color: #ffffff;
    font-family: var(--font-sans);
    font-size: 1rem;
    outline: none;
    transition: border-color 0.2s;
}

#dial-input:focus {
    border-color: var(--secondary);
}

#dial-input::placeholder {
    color: var(--text-muted);
}

.dial-error {
    margin-top: 10px;
    font-size: 0.85rem;
    color: var(--warning);
}
```
(All four custom properties — `--font-sans`, `--secondary`, `--text-muted`, `--warning` — already exist in this stylesheet.)

- [ ] **Step 5: Static sanity check**

Run: `node --check frontend/app.js` (skip without failing if node isn't installed — the repo has no JS toolchain; this is a free syntax check, not a gate). Also grep to prove the removal is complete:
```bash
grep -n "LEADS\|renderLeads\|lead-list\|lead-item\|lead-info\|lead-name-text\|lead-company-text" frontend/app.js frontend/index.html frontend/style.css
```
Expected: no matches.

- [ ] **Step 6: Manual browser verification**

Run `uvicorn backend.main:app --port 8000`, open `http://localhost:8000`, **hard refresh** (cached `app.js`):
1. Left card shows the Dialer (no leads list).
2. Type `6281686616` → error line stays hidden... then click Call → active-call console appears showing `+916281686616` as both name and number (the backend call itself may 503 if the GPU is cold — that's the warm gate, not this feature; the UI transition + request payload is what's being verified).
3. Type `abc123` → Call → inline error, no request in the network tab.
4. Type `+15557778888`, press **Enter** → dials as-is.
5. During an active/attempted call the Call button is greyed out (disabled), re-enabled after reset.

- [ ] **Step 7: Commit**

```bash
git add frontend/index.html frontend/app.js frontend/style.css
git commit -m "feat(dashboard): manual dialer replaces mock leads directory"
```

---

## Self-Review

**Spec coverage:** removals (LEADS, renderLeads, init call, lead-list markup, dead CSS) → Steps 1/2/4; dialer UI + normalization rules + Enter key + inline error → Steps 1/3; number-as-name → `startOutboundCall(number, number)` in Step 3; disable-during-call → satisfied via the `.call-action-btn` class reuse (deviation documented in the header); zero backend changes → no backend file appears in any step; verification → Steps 5-6 (manual, per repo convention). **Placeholder scan:** none — every step carries literal code. **Type consistency:** `normalizeDialNumber`/`dialManualNumber`/`dial-input`/`btn-dial`/`dial-error` used identically across Steps 1, 3, 4.
