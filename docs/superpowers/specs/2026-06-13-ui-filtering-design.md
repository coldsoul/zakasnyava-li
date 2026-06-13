# UI Filtering — Design Spec
Date: 2026-06-13

## Feature summary

Add two client-side filters to the ranking table on `index.astro`:
1. Category chips (bus / metro / tram / trolleybus) — single-select with "Всички" reset
2. Live search box — filters by line name/number

Frontend-only. No backend or data changes.

## Layout

Filter bar inserted between `.ranking-controls` (month picker) and `.table-wrap` (the table). Dedicated row — month picker stays in its own row above; filter chips + search share the filter bar row.

```
┌─────────────────────────────────────────────────┐
│ Надеждност на градски транспорт — София         │
│ [юни 2026 ▼]                   ← controls row  │
│ [Всички] [автобус] [метро] [трамвай] [тролей]  │
│                                    [🔍 Линия…]  │  ← filter bar
├─────────────────────────────────────────────────┤
│ Линия │ Вид │ Медиана │ P90 │ …                │
│  …                                              │
└─────────────────────────────────────────────────┘
```

## HTML additions

```astro
<div class="filter-bar" id="filter-bar">
  <div class="type-chips" id="type-chips">
    <button class="chip active" data-type="all">Всички</button>
    <button class="chip" data-type="bus">автобус</button>
    <button class="chip" data-type="metro">метро</button>
    <button class="chip" data-type="tram">трамвай</button>
    <button class="chip" data-type="trolleybus">тролей</button>
  </div>
  <input class="search-input" id="line-search" type="search"
         placeholder="Линия…" aria-label="Търсене на линия" />
</div>
```

All four type chips always rendered. If no rows match a selected type, table shows an empty-state row.

## JS architecture

### Data injection

Pass SSR data into client script via Astro's `define:vars`:

```astro
<script define:vars={{ initialRows: rows, latestMonth }}>
```

### State

```js
const state = {
  rows: initialRows,   // full dataset for current month
  month: latestMonth,
  activeType: 'all',   // 'all' | 'bus' | 'metro' | 'tram' | 'trolleybus'
  query: '',
};
```

### Core functions

**`filteredRows()`** — pure filter, no side effects:
```js
function filteredRows() {
  const q = state.query.toLowerCase().trim();
  return state.rows.filter(r => {
    const typeMatch = state.activeType === 'all' || r.type === state.activeType;
    const nameMatch = !q || r.name.toLowerCase().includes(q);
    return typeMatch && nameMatch;
  });
}
```

**`render()`** — rebuilds tbody:
```js
function render() {
  const tbody = document.querySelector('#ranking-table tbody');
  if (!tbody) return;
  const filtered = filteredRows();
  tbody.innerHTML = filtered.length
    ? filtered.map(r => rowHtml(r, state.month)).join('')
    : '<tr><td colspan="8" class="empty-state">Няма линии.</td></tr>';
}
```

**`setType(type)`** — updates chip state and re-renders:
```js
function setType(type) {
  state.activeType = type;
  document.querySelectorAll('#type-chips .chip').forEach(c =>
    c.classList.toggle('active', c.dataset.type === type)
  );
  render();
}
```

### Event wiring

| Event | Handler |
|-------|---------|
| Chip `click` | `setType(chip.dataset.type)` |
| Search `input` | debounced (150ms): set `state.query`, call `render()` |
| Month `select` change | fetch new JSON → set `state.rows` + `state.month` → call `render()`, update heading |
| `DOMContentLoaded` | call `render()` once (JS takes over from SSR) |

Existing helpers `rowHtml()`, `trendStr()`, `trendCls()`, `TYPE_BG` unchanged.

## CSS additions (in `index.astro` `<style>`)

```css
.filter-bar {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin: 0.75rem 0;
  flex-wrap: wrap;
}
.type-chips {
  display: flex;
  gap: 0.4rem;
  flex-wrap: wrap;
}
.chip {
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 20px;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 0.8rem;
  padding: 3px 12px;
}
.chip.active {
  background: #eef2ff;
  border-color: var(--primary);
  color: var(--primary);
}
.search-input {
  border: 1px solid var(--border);
  border-radius: 4px;
  background: transparent;
  color: inherit;
  font-size: 0.85rem;
  margin-left: auto;
  padding: 3px 8px;
  width: 9rem;
}
.empty-state {
  color: var(--text-muted);
  padding: 2rem;
  text-align: center;
}
```

## Behaviour edge cases

- **Month change with active filter**: `state.activeType` and `state.query` persist — user keeps their filter across month switches.
- **Empty result**: single colspan=8 row with "Няма линии." message.
- **No-JS**: SSR renders all rows; filter bar present but non-functional. Acceptable — filtering inherently requires JS.
- **Type with zero rows**: chip always visible; selecting it shows empty-state message.

## Files changed

- `site/src/pages/index.astro` — HTML additions, script refactor, style additions

## Branch

`epic/filter-ui`
