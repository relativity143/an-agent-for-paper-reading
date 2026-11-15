// 两栏的布局：只看原文、并排、只看讲解三种；并排时可以拖中缝调宽度。
// 选择记在这台电脑的浏览器里（localStorage），只是界面偏好，不涉及阅读记录。

const VIEW_KEY = 'paperread-view';
const SPLIT_KEY = 'paperread-split';
const VIEWS = ['paper', 'both', 'agent'];
const MIN_SPLIT = 25;
const MAX_SPLIT = 75;
const DEFAULT_SPLIT = 56;

function load(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}

function save(key, value) {
  try { localStorage.setItem(key, value); } catch { /* 记不下来时只是下次回到默认 */ }
}

export function createLayout({ layout, divider, switcher }) {
  let view = VIEWS.includes(load(VIEW_KEY)) ? load(VIEW_KEY) : 'both';
  let split = Number(load(SPLIT_KEY));
  if (!Number.isFinite(split) || split < MIN_SPLIT || split > MAX_SPLIT) split = DEFAULT_SPLIT;

  function applySplit() {
    layout.style.setProperty('--split', `${split}%`);
    divider.setAttribute('aria-valuenow', String(Math.round(split)));
  }

  function setSplit(value, remember = true) {
    split = Math.min(MAX_SPLIT, Math.max(MIN_SPLIT, value));
    applySplit();
    if (remember) save(SPLIT_KEY, String(Math.round(split * 10) / 10));
  }

  function setView(next) {
    view = VIEWS.includes(next) ? next : 'both';
    layout.dataset.view = view;
    for (const button of switcher.querySelectorAll('[data-view]')) {
      button.setAttribute('aria-pressed', String(button.dataset.view === view));
    }
    save(VIEW_KEY, view);
  }

  switcher.addEventListener('click', (event) => {
    const button = event.target.closest('[data-view]');
    if (button) setView(button.dataset.view);
  });

  // 拖中缝：按住拖动改左边的宽度，松开后记住。双击回到默认。
  divider.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    try { divider.setPointerCapture(event.pointerId); } catch { /* 抓不住指针时仍然能拖，只是移出中缝后不跟手 */ }
    layout.classList.add('dragging');
    const move = (e) => {
      const box = layout.getBoundingClientRect();
      if (box.width > 0) setSplit(((e.clientX - box.left) / box.width) * 100, false);
    };
    const stop = () => {
      divider.removeEventListener('pointermove', move);
      divider.removeEventListener('pointerup', stop);
      divider.removeEventListener('pointercancel', stop);
      layout.classList.remove('dragging');
      setSplit(split);
    };
    divider.addEventListener('pointermove', move);
    divider.addEventListener('pointerup', stop);
    divider.addEventListener('pointercancel', stop);
  });
  divider.addEventListener('dblclick', () => setSplit(DEFAULT_SPLIT));
  divider.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowLeft') { event.preventDefault(); setSplit(split - 2); }
    if (event.key === 'ArrowRight') { event.preventDefault(); setSplit(split + 2); }
    if (event.key === 'Home') { event.preventDefault(); setSplit(DEFAULT_SPLIT); }
  });

  applySplit();
  setView(view);

  return {
    view: () => view,
    setView,
    // 讲解里点了原文位置、而原文那一边收着时，把它放出来。
    showPaper() { if (view === 'agent') setView('both'); },
    // 要提问、而讲解那一边收着时，把它放出来。
    showAgent() { if (view === 'paper') setView('both'); },
  };
}
