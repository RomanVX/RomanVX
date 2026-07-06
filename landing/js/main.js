/* Market Partners — лендинг: скролл, анимации, калькулятор, форма */
(function () {
  'use strict';

  var reducedMotionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
  function reducedMotion() { return reducedMotionQuery.matches; }
  function isMobile() { return window.innerWidth < 768; }

  /* ---------- Плавный скролл (Lenis) ---------- */

  var lenis = null;
  if (typeof window.Lenis === 'function' && !reducedMotion()) {
    lenis = new window.Lenis({ lerp: 0.1 });
    var lenisRaf = function (time) {
      lenis.raf(time);
      window.requestAnimationFrame(lenisRaf);
    };
    window.requestAnimationFrame(lenisRaf);
  }

  /* ---------- Шапка, бургер-меню ---------- */

  var header = document.querySelector('.header');
  var burger = document.querySelector('.burger');

  function menuIsOpen() { return document.body.classList.contains('menu-open'); }

  function openMenu() {
    document.body.classList.add('menu-open');
    burger.setAttribute('aria-expanded', 'true');
    burger.setAttribute('aria-label', 'Закрыть меню');
    if (lenis) lenis.stop();
  }

  function closeMenu() {
    document.body.classList.remove('menu-open');
    burger.setAttribute('aria-expanded', 'false');
    burger.setAttribute('aria-label', 'Открыть меню');
    if (lenis) lenis.start();
  }

  if (burger) {
    burger.addEventListener('click', function () {
      if (menuIsOpen()) { closeMenu(); } else { openMenu(); }
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && menuIsOpen()) {
        closeMenu();
        burger.focus();
      }
    });
    // при развороте окна на десктоп бургер исчезает — меню не должно остаться открытым
    window.addEventListener('resize', function () {
      if (window.innerWidth > 1024 && menuIsOpen()) closeMenu();
    });
  }

  /* ---------- Якорные ссылки ---------- */

  var HEADER_OFFSET = -84;

  document.querySelectorAll('a[href^="#"]').forEach(function (link) {
    link.addEventListener('click', function (e) {
      var hash = link.getAttribute('href');
      if (!hash || hash.length < 2) return;
      var target = document.querySelector(hash);
      if (!target) return;
      e.preventDefault();
      var wasMenu = menuIsOpen();
      closeMenu();
      if (lenis) {
        lenis.scrollTo(target, {
          offset: hash === '#top' ? 0 : HEADER_OFFSET,
          // после оверлея даём меню закрыться, чтобы скролл не дёргался
          lock: false
        });
      } else {
        target.scrollIntoView({ behavior: reducedMotion() || wasMenu ? 'auto' : 'smooth' });
      }
      if (history.pushState) history.pushState(null, '', hash);
    });
  });

  /* ---------- Прогресс-индикатор скролла и состояние шапки ---------- */

  var progressBar = document.querySelector('.scroll-progress');

  function onScroll() {
    var max = document.documentElement.scrollHeight - window.innerHeight;
    var p = max > 0 ? Math.min(1, window.scrollY / max) : 0;
    if (progressBar) progressBar.style.transform = 'scaleX(' + p + ')';
    if (header) header.classList.toggle('is-scrolled', window.scrollY > 8);
  }

  window.addEventListener('scroll', onScroll, { passive: true });
  window.addEventListener('resize', onScroll, { passive: true });
  onScroll();

  /* ---------- Появление секций (IntersectionObserver) ---------- */

  var revealEls = document.querySelectorAll('.reveal');

  if ('IntersectionObserver' in window && !reducedMotion()) {
    var revealObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          revealObserver.unobserve(entry.target);
        }
      });
    }, { threshold: 0.15 });
    revealEls.forEach(function (el) { revealObserver.observe(el); });
  } else {
    revealEls.forEach(function (el) { el.classList.add('is-visible'); });
  }

  /* ---------- Числа-счётчики ---------- */

  var counters = document.querySelectorAll('[data-counter]');

  function easeOutCubic(t) { return 1 - Math.pow(1 - t, 3); }

  function formatCounter(n) {
    return Math.round(n).toLocaleString('ru-RU');
  }

  function runCounter(el) {
    var target = parseFloat(el.getAttribute('data-counter'));
    if (!isFinite(target)) return;
    var duration = 1200;
    var startTime = null;
    function frame(ts) {
      if (startTime === null) startTime = ts;
      var t = Math.min(1, (ts - startTime) / duration);
      el.textContent = formatCounter(target * easeOutCubic(t));
      if (t < 1) window.requestAnimationFrame(frame);
    }
    window.requestAnimationFrame(frame);
  }

  if (counters.length) {
    if (!reducedMotion() && 'IntersectionObserver' in window) {
      counters.forEach(function (el) { el.textContent = '0'; });
      var counterObserver = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            runCounter(entry.target);
            counterObserver.unobserve(entry.target);
          }
        });
      }, { threshold: 0.4 });
      counters.forEach(function (el) { counterObserver.observe(el); });
    }
    // без JS/с reduced-motion в разметке уже стоят финальные значения
  }

  /* ---------- Линия этапов процесса ---------- */

  var processSteps = document.querySelector('.process__steps');
  if (processSteps && 'IntersectionObserver' in window && !reducedMotion()) {
    var processObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          processObserver.unobserve(entry.target);
        }
      });
    }, { threshold: 0.2 });
    processObserver.observe(processSteps);
  } else if (processSteps) {
    processSteps.classList.add('is-visible');
  }

  /* ---------- Canvas-сетка в hero ---------- */

  (function initHeroCanvas() {
    var canvas = document.querySelector('.hero__canvas');
    if (!canvas || !canvas.getContext) return;
    var ctx = canvas.getContext('2d');
    var hero = canvas.closest('.hero');
    if (!ctx || !hero) return;

    var width = 0;
    var height = 0;
    var points = [];
    var linkDistance = 180;
    var rafId = null;
    var heroVisible = true;
    var scrollFactor = 0;
    var lastBuildWidth = 0;

    function build() {
      var dpr = Math.min(2, window.devicePixelRatio || 1);
      width = hero.offsetWidth;
      height = hero.offsetHeight;
      lastBuildWidth = window.innerWidth;
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      canvas.style.width = width + 'px';
      canvas.style.height = height + 'px';
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      var count = isMobile() ? 16 : 40;
      linkDistance = Math.max(130, Math.min(230, Math.hypot(width, height) / 8));
      points = [];
      for (var i = 0; i < count; i++) {
        points.push({
          x: Math.random() * width,
          y: Math.random() * height,
          vx: (Math.random() - 0.5) * 0.24,
          vy: (Math.random() - 0.5) * 0.24,
          r: 1.2 + Math.random() * 1.4
        });
      }
    }

    function draw() {
      ctx.clearRect(0, 0, width, height);

      // при прокрутке сетка слегка «разъезжается» и уплывает вниз
      var spread = 1 + scrollFactor * 0.18;
      var offsetY = scrollFactor * height * 0.12;
      var cx = width / 2;
      var cy = height / 2;

      var projected = [];
      for (var i = 0; i < points.length; i++) {
        projected.push({
          x: cx + (points[i].x - cx) * spread,
          y: cy + (points[i].y - cy) * spread + offsetY,
          r: points[i].r
        });
      }

      ctx.lineWidth = 1;
      for (var a = 0; a < projected.length; a++) {
        for (var b = a + 1; b < projected.length; b++) {
          var dx = projected[a].x - projected[b].x;
          var dy = projected[a].y - projected[b].y;
          var dist = Math.sqrt(dx * dx + dy * dy);
          if (dist < linkDistance) {
            var alpha = 0.08 + (1 - dist / linkDistance) * 0.07;
            ctx.strokeStyle = 'rgba(26,43,74,' + alpha.toFixed(3) + ')';
            ctx.beginPath();
            ctx.moveTo(projected[a].x, projected[a].y);
            ctx.lineTo(projected[b].x, projected[b].y);
            ctx.stroke();
          }
        }
      }

      ctx.fillStyle = 'rgba(26,43,74,0.16)';
      for (var d = 0; d < projected.length; d++) {
        ctx.beginPath();
        ctx.arc(projected[d].x, projected[d].y, projected[d].r, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    function tick() {
      for (var i = 0; i < points.length; i++) {
        var p = points[i];
        p.x += p.vx;
        p.y += p.vy;
        if (p.x < -24) p.x = width + 24; else if (p.x > width + 24) p.x = -24;
        if (p.y < -24) p.y = height + 24; else if (p.y > height + 24) p.y = -24;
      }
      draw();
      rafId = window.requestAnimationFrame(tick);
    }

    function start() {
      if (rafId === null && heroVisible && !document.hidden && !reducedMotion()) {
        rafId = window.requestAnimationFrame(tick);
      }
    }

    function stop() {
      if (rafId !== null) {
        window.cancelAnimationFrame(rafId);
        rafId = null;
      }
    }

    function updateScrollFactor() {
      // параллакс только на десктопе
      if (isMobile() || reducedMotion()) { scrollFactor = 0; return; }
      var heroHeight = hero.offsetHeight || 1;
      scrollFactor = Math.max(0, Math.min(1, window.scrollY / heroHeight));
    }

    build();

    if (reducedMotion()) {
      draw(); // статичный кадр вместо анимации
    } else {
      start();
    }

    window.addEventListener('scroll', updateScrollFactor, { passive: true });

    var resizeTimer = null;
    window.addEventListener('resize', function () {
      // на мобильных высота меняется из-за адресной строки — реагируем только на ширину
      if (window.innerWidth === lastBuildWidth) return;
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () {
        build();
        updateScrollFactor();
        if (reducedMotion()) draw();
      }, 200);
    });

    if ('IntersectionObserver' in window) {
      new IntersectionObserver(function (entries) {
        heroVisible = entries[0].isIntersecting;
        if (heroVisible) { start(); } else { stop(); }
      }).observe(hero);
    }

    document.addEventListener('visibilitychange', function () {
      if (document.hidden) { stop(); } else { start(); }
    });

    var onMotionChange = function () {
      if (reducedMotion()) { stop(); scrollFactor = 0; draw(); } else { start(); }
    };
    if (reducedMotionQuery.addEventListener) {
      reducedMotionQuery.addEventListener('change', onMotionChange);
    }
  })();

  /* ---------- Калькулятор юнит-экономики ---------- */

  (function initCalculator() {
    var root = document.querySelector('.calc');
    if (!root) return;

    var el = function (id) { return document.getElementById(id); };
    var inputCost = el('calc-cost');
    var inputPrice = el('calc-price');
    var inputFee = el('calc-fee');
    var inputLogistics = el('calc-logistics');
    var inputAdv = el('calc-adv');
    var inputTax = el('calc-tax');
    var outProfit = el('calc-profit');
    var outMargin = el('calc-margin');
    var outMin = el('calc-min');
    var note = el('calc-note');
    if (!inputCost || !inputPrice || !outProfit) return;

    function num(input) {
      var v = parseFloat(String(input.value).replace(',', '.'));
      return isFinite(v) && v >= 0 ? v : 0;
    }

    function money(n) {
      return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 }).format(Math.round(n)) + ' ₽';
    }

    function update() {
      var cost = num(inputCost);
      var price = num(inputPrice);
      var feeShare = num(inputFee) / 100;
      var logistics = num(inputLogistics);
      var advShare = num(inputAdv) / 100;
      var taxShare = num(inputTax) / 100;

      var fee = price * feeShare;
      var adv = price * advShare;
      var tax = price * taxShare;
      var profit = price - cost - fee - logistics - adv - tax;
      var margin = price > 0 ? (profit / price) * 100 : 0;

      // цена_min = (себестоимость + логистика) / (1 − комиссия% − ДРР% − налог%);
      // эпсилон гасит ошибку плавающей точки, когда сборы дают ровно 100%
      var denominator = 1 - feeShare - advShare - taxShare;
      var breakeven = denominator > 1e-9 ? (cost + logistics) / denominator : null;

      outProfit.textContent = money(profit);
      outMargin.textContent = margin.toLocaleString('ru-RU', { maximumFractionDigits: 1 }) + '%';
      outMin.textContent = breakeven !== null ? money(breakeven) : '—';

      var negative = profit < 0;
      root.classList.toggle('calc--negative', negative);
      if (note) note.hidden = !negative;
    }

    [inputCost, inputPrice, inputFee, inputLogistics, inputAdv, inputTax].forEach(function (input) {
      if (input) input.addEventListener('input', update);
    });

    update();
  })();

  /* ---------- Форма заявки ---------- */

  (function initForm() {
    var form = document.getElementById('audit-form');
    if (!form) return;

    var button = form.querySelector('.audit__submit');
    var label = form.querySelector('.btn__label');
    var status = document.getElementById('form-status');
    var defaultLabel = label ? label.textContent : '';
    var resetTimer = null;

    function setStatus(text, isError) {
      if (!status) return;
      status.textContent = text;
      status.classList.toggle('is-error', Boolean(isError));
    }

    function setButton(text, disabled) {
      if (label) label.textContent = text;
      if (button) button.disabled = disabled;
    }

    function restoreLater() {
      if (resetTimer) clearTimeout(resetTimer);
      resetTimer = setTimeout(function () {
        setButton(defaultLabel, false);
      }, 4000);
    }

    // после отправки без JS PHP возвращает на index.html?sent=1#audit
    if (/[?&]sent=1/.test(window.location.search)) {
      setStatus('Заявка отправлена. Ответим в течение рабочего дня.');
    } else if (/[?&]sent=0/.test(window.location.search)) {
      setStatus('Не удалось отправить заявку. Напишите нам напрямую: hello@marketpartners.ru', true);
    }

    form.addEventListener('submit', function (e) {
      e.preventDefault();

      if (!form.checkValidity()) {
        form.reportValidity();
        return;
      }

      setStatus('');
      setButton('Отправляем…', true);

      fetch(form.action, {
        method: 'POST',
        body: new FormData(form),
        headers: { 'X-Requested-With': 'fetch', 'Accept': 'application/json' }
      })
        .then(function (res) { return res.json(); })
        .then(function (data) {
          if (!data || data.ok !== true) throw new Error('send failed');
          setButton('Отправлено ✓', true);
          setStatus('Заявка отправлена. Ответим в течение рабочего дня.');
          form.reset();
          restoreLater();
        })
        .catch(function () {
          setButton(defaultLabel, false);
          setStatus('Не удалось отправить заявку. Попробуйте ещё раз или напишите на hello@marketpartners.ru', true);
        });

      /* ── Альтернатива: отправка через Formspree (если хостинг без PHP) ──
      1. Зарегистрируйтесь на https://formspree.io и создайте форму.
      2. Замените YOUR_FORM_ID на идентификатор формы.
      3. Закомментируйте fetch на send.php выше и раскомментируйте блок ниже.

      fetch('https://formspree.io/f/YOUR_FORM_ID', {
        method: 'POST',
        body: new FormData(form),
        headers: { 'Accept': 'application/json' }
      })
        .then(function (res) {
          if (!res.ok) throw new Error('send failed');
          setButton('Отправлено ✓', true);
          setStatus('Заявка отправлена. Ответим в течение рабочего дня.');
          form.reset();
          restoreLater();
        })
        .catch(function () {
          setButton(defaultLabel, false);
          setStatus('Не удалось отправить заявку. Попробуйте ещё раз.', true);
        });
      ─────────────────────────────────────────────────────────────── */
    });
  })();

})();
