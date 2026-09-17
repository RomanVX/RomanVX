/* Market Partners — /fulfilment
   Interactivity ported from the Claude Design prototype (DCLogic component). */
(() => {
  'use strict';

  const fmt = n => n.toLocaleString('ru-RU');
  const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;

  // Gates the "hidden until revealed" styles so the page stays fully visible without JS.
  document.documentElement.classList.add('js');

  /* Animated counter: numbers chase their target through intermediate values
     (100 → 101 → … → 200) instead of jumping. Restartable mid-flight, so
     dragging the slider keeps the count smooth. */
  const counters = new WeakMap();
  function setNumber(el, target, instant) {
    const st = counters.get(el) || { current: target, raf: 0 };
    counters.set(el, st);
    cancelAnimationFrame(st.raf);
    if (instant || reduceMotion) {
      st.current = target;
      el.textContent = fmt(target);
      return;
    }
    const from = st.current;
    const start = performance.now();
    const dur = 400;
    const ease = t => 1 - Math.pow(1 - t, 3);
    const tick = now => {
      const t = Math.min((now - start) / dur, 1);
      st.current = from + (target - from) * ease(t);
      el.textContent = fmt(Math.round(st.current));
      if (t < 1) st.raf = requestAnimationFrame(tick);
    };
    st.raf = requestAnimationFrame(tick);
  }

  /* ---------- Calculator (02) ---------- */
  // Weight tiers: [label, surcharge coefficient]; null = "свыше 8 кг" → individual quote.
  const WEIGHTS = [
    ['до 1 кг', 0],
    ['1–3 кг', 0.2],
    ['3–8 кг', 0.4],
    ['свыше 8 кг', null],
  ];
  const baseRate = o =>
    o < 500 ? 55 : o < 1000 ? 50 : o < 2000 ? 45 : o < 3000 ? 40 : o < 4000 ? 35 : 30;

  const calc = { orders: 100, weight: 0, fragile: false, marketplaces: { wb: false, ozon: false } };

  const ordersInput = document.getElementById('orders');
  const ordersValue = document.getElementById('orders-value');
  const weightsWrap = document.getElementById('weights');
  const fragileInput = document.getElementById('fragile');
  const resultEl = document.getElementById('calc-result');
  const over8El = document.getElementById('calc-over8');
  const rateEl = document.getElementById('rate');
  const monthEl = document.getElementById('month');

  const weightBtns = WEIGHTS.map(([label], i) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'weight-btn';
    btn.textContent = label;
    btn.addEventListener('click', () => {
      calc.weight = i;
      renderCalc();
    });
    weightsWrap.appendChild(btn);
    return btn;
  });

  let calcReady = false;
  function renderCalc() {
    const k = WEIGHTS[calc.weight][1];
    const over8 = k === null;
    setNumber(ordersValue, calc.orders, !calcReady);
    weightBtns.forEach((btn, i) => btn.classList.toggle('is-active', i === calc.weight));
    resultEl.hidden = over8;
    over8El.hidden = !over8;
    if (!over8) {
      const rate = Math.round(baseRate(calc.orders) * (1 + k) * (calc.fragile ? 1.15 : 1));
      setNumber(rateEl, rate, !calcReady);
      setNumber(monthEl, rate * calc.orders, !calcReady); // per-day estimate
    }
  }

  ordersInput.addEventListener('input', () => {
    calc.orders = +ordersInput.value;
    renderCalc();
  });
  fragileInput.addEventListener('change', () => {
    calc.fragile = fragileInput.checked;
    renderCalc();
  });
  // Marketplace choice doesn't affect the rate — it's context we attach to the request.
  [['mp-wb', 'wb'], ['mp-ozon', 'ozon']].forEach(([id, key]) => {
    const input = document.getElementById(id);
    input.addEventListener('change', () => { calc.marketplaces[key] = input.checked; });
  });
  renderCalc();
  calcReady = true;

  document.getElementById('price-date').textContent = new Date().toLocaleDateString('ru-RU', {
    day: 'numeric',
    month: 'long',
    year: 'numeric',
  });

  /* ---------- Cabinet (04): tabs on desktop, accordion on mobile ---------- */
  // Two independent states, as in the prototype (cab / cabAcc); CSS media queries
  // decide which one drives panel visibility.
  const cabItems = [...document.querySelectorAll('[data-cab]')];
  const cabTabsWrap = document.getElementById('cab-tabs');

  const cabTabBtns = cabItems.map((item, i) => {
    const title = item.querySelector('.acc-btn span').textContent;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'tab-btn';
    btn.setAttribute('role', 'tab');
    btn.textContent = title;
    btn.addEventListener('click', () => setCabTab(i));
    cabTabsWrap.appendChild(btn);
    return btn;
  });

  function setCabTab(active) {
    cabItems.forEach((item, i) => item.classList.toggle('is-tab-active', i === active));
    cabTabBtns.forEach((btn, i) => {
      btn.classList.toggle('is-active', i === active);
      btn.setAttribute('aria-selected', String(i === active));
    });
  }

  function setCabAccordion(open) {
    cabItems.forEach((item, i) => {
      const isOpen = i === open;
      item.classList.toggle('is-acc-open', isOpen);
      const btn = item.querySelector('.acc-btn');
      btn.setAttribute('aria-expanded', String(isOpen));
      btn.querySelector('.acc-sign').textContent = isOpen ? '−' : '+';
    });
  }

  cabItems.forEach((item, i) => {
    item.querySelector('.acc-btn').addEventListener('click', () => {
      setCabAccordion(item.classList.contains('is-acc-open') ? -1 : i);
    });
  });

  setCabTab(0);
  setCabAccordion(0);

  /* ---------- Migration tabs (05) ---------- */
  const migTabs = [...document.querySelectorAll('[data-mig-tab]')];
  const migPanels = [...document.querySelectorAll('[data-mig-panel]')];
  migTabs.forEach(tab => {
    tab.addEventListener('click', () => {
      const key = tab.dataset.migTab;
      migTabs.forEach(t => {
        const active = t === tab;
        t.classList.toggle('is-active', active);
        t.setAttribute('aria-selected', String(active));
      });
      migPanels.forEach(p => { p.hidden = p.dataset.migPanel !== key; });
    });
  });

  /* ---------- FAQ (09): one open at a time, first open by default ---------- */
  const faqItems = [...document.querySelectorAll('[data-faq]')];
  function setFaq(open) {
    faqItems.forEach((item, i) => {
      const isOpen = i === open;
      const btn = item.querySelector('.acc-btn');
      btn.setAttribute('aria-expanded', String(isOpen));
      btn.querySelector('.acc-sign').textContent = isOpen ? '−' : '+';
      item.querySelector('.faq-answer').hidden = !isOpen;
    });
  }
  faqItems.forEach((item, i) => {
    item.querySelector('.acc-btn').addEventListener('click', () => {
      const isOpen = item.querySelector('.acc-btn').getAttribute('aria-expanded') === 'true';
      setFaq(isOpen ? -1 : i);
    });
  });
  setFaq(0);

  /* ---------- CTA topic chips ---------- */
  const chips = [...document.querySelectorAll('.chip')];
  chips.forEach(chip => {
    chip.addEventListener('click', () => {
      chips.forEach(c => {
        const active = c === chip;
        c.classList.toggle('is-active', active);
        c.setAttribute('aria-pressed', String(active));
      });
    });
  });

  /* ---------- Menu overlay ---------- */
  const menu = document.getElementById('menu');
  const openBtn = document.getElementById('menu-open');
  const closeBtn = document.getElementById('menu-close');
  const setMenu = open => {
    menu.hidden = !open;
    document.body.style.overflow = open ? 'hidden' : '';
  };
  openBtn.addEventListener('click', () => setMenu(true));
  closeBtn.addEventListener('click', () => setMenu(false));
  menu.querySelectorAll('[data-menu-close]').forEach(a =>
    a.addEventListener('click', () => setMenu(false))
  );
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !menu.hidden) setMenu(false);
  });

  /* ---------- Forms → /api/landing/lead (Telegram) ---------- */
  const TOPIC_KEYS = { 'Расчёт тарифа': 'calc', 'Расчёт': 'calc', 'Консультация': 'consult', 'Переезд': 'move' };
  document.querySelectorAll('[data-form]').forEach(form => {
    form.addEventListener('submit', async e => {
      e.preventDefault();
      const note = form.querySelector('[data-form-note]');
      const btn = form.querySelector('button[type="submit"]');
      const contact = (form.elements.contact?.value || '').trim();
      if (!contact) { note.hidden = false; note.textContent = 'Укажите телефон или Telegram.'; return; }
      const isCalc = form.classList.contains('calc-form');
      const chip = form.querySelector('.chip.is-active');
      const topic = isCalc ? 'fix' : (TOPIC_KEYS[(chip?.textContent || '').trim()] || 'consult');
      const payload = {
        name: (form.elements.name?.value || '').trim(),
        contact, topic, form: isCalc ? 'calc' : 'cta',
        website: form.elements.website?.value || '',
      };
      if (isCalc) {
        const k = WEIGHTS[calc.weight][1];
        payload.calc = {
          orders: calc.orders, weight: WEIGHTS[calc.weight][0], fragile: calc.fragile,
          rate: k === null ? '' : Math.round(baseRate(calc.orders) * (1 + k) * (calc.fragile ? 1.15 : 1)),
          month: k === null ? 'индивидуально' : Math.round(baseRate(calc.orders) * (1 + k) * (calc.fragile ? 1.15 : 1)) * calc.orders * 30,
          marketplaces: Object.keys(calc.marketplaces).filter(m => calc.marketplaces[m]).join(', '),
        };
      }
      btn.disabled = true; note.hidden = false; note.textContent = 'Отправляем…';
      try {
        const r = await fetch('/api/landing/lead', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        const j = await r.json().catch(() => ({}));
        if (r.ok && j.ok) { note.textContent = 'Заявка отправлена. Ответим в рабочий день.'; form.reset(); }
        else note.textContent = j.error || 'Не удалось отправить, напишите нам в Telegram.';
      } catch (err) {
        note.textContent = 'Не удалось отправить, напишите нам в Telegram.';
      } finally { btn.disabled = false; }
    });
  });

  /* ---------- Hero subtitle: "для" stays, marketplace names retype ---------- */
  // The full static line stays in the HTML for no-JS/reduced-motion and SEO.
  const heroSub = document.querySelector('.hero-sub');
  if (heroSub && !reduceMotion) {
    const NAMES = ['Wildberries', 'Ozon'];
    const HOLD = 2200, TYPE = 70, ERASE = 38, GAP = 350;
    heroSub.textContent = 'для ';
    const word = document.createElement('span');
    word.className = 'type-word';
    word.textContent = NAMES[0];
    const caret = document.createElement('span');
    caret.className = 'type-caret';
    caret.setAttribute('aria-hidden', 'true');
    heroSub.append(word, caret);
    let idx = 0;
    const erase = () => {
      if (word.textContent) {
        word.textContent = word.textContent.slice(0, -1);
        setTimeout(erase, ERASE);
      } else {
        idx = (idx + 1) % NAMES.length;
        setTimeout(type, GAP);
      }
    };
    const type = () => {
      const name = NAMES[idx];
      word.textContent = name.slice(0, word.textContent.length + 1);
      if (word.textContent.length < name.length) setTimeout(type, TYPE);
      else setTimeout(erase, HOLD);
    };
    setTimeout(erase, HOLD);
  }

  /* ---------- Scroll reveals: [data-stagger] children fade/slide in one by one ---------- */
  document.querySelectorAll('[data-stagger]').forEach(group => {
    [...group.children].forEach((child, i) => {
      child.classList.add('reveal');
      if (group.dataset.stagger === 'photo') child.classList.add('reveal-photo');
      child.style.setProperty('--rd', i * 90 + 'ms');
    });
  });
  const io = new IntersectionObserver(
    entries => {
      entries.forEach(e => {
        if (e.isIntersecting) {
          e.target.classList.add('is-in');
          io.unobserve(e.target);
        }
      });
    },
    { rootMargin: '0px 0px -8% 0px', threshold: 0.1 }
  );
  document.querySelectorAll('.reveal').forEach(el => io.observe(el));

  /* ---------- Hero photos: hidden on load, rise into place as you scroll ---------- */
  const heroStrip = document.querySelector('.hero-photos');
  if (heroStrip && !reduceMotion) {
    const photos = [...heroStrip.querySelectorAll('.photo')];
    let done = false;
    const update = () => {
      if (done) return;
      const r = heroStrip.getBoundingClientRect();
      const p = Math.min(Math.max((innerHeight - r.top) / (innerHeight * 0.55), 0), 1);
      photos.forEach((ph, i) => {
        const q = Math.min(Math.max(p * 1.3 - i * 0.1, 0), 1);
        const e = 1 - Math.pow(1 - q, 2);
        ph.style.opacity = e;
        ph.style.transform = `translateY(${(1 - e) * 64}px) scale(${0.92 + e * 0.08})`;
      });
      if (p >= 1) {
        photos.forEach(ph => {
          ph.style.opacity = '';
          ph.style.transform = '';
          ph.classList.add('is-in');
        });
        done = true;
      }
    };
    addEventListener('scroll', () => requestAnimationFrame(update), { passive: true });
    addEventListener('resize', update);
    update();
  }
})();
