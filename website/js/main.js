/* Ashlr AO — Main JS: Mission Control */

// Theme toggle
function initTheme() {
  const saved = localStorage.getItem('ashlr-theme');
  if (saved) {
    document.documentElement.setAttribute('data-theme', saved);
  } else if (window.matchMedia('(prefers-color-scheme: light)').matches) {
    document.documentElement.setAttribute('data-theme', 'light');
  }
}

function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme');
  const next = current === 'light' ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('ashlr-theme', next);
}

// Mobile nav
function initMobileNav() {
  const toggle = document.querySelector('.nav-mobile-toggle');
  const links = document.querySelector('.nav-links');
  if (!toggle || !links) return;

  toggle.addEventListener('click', () => {
    links.classList.toggle('open');
    toggle.setAttribute('aria-expanded', links.classList.contains('open'));
  });

  links.querySelectorAll('a').forEach(a => {
    a.addEventListener('click', () => links.classList.remove('open'));
  });

  document.addEventListener('click', (e) => {
    if (!toggle.contains(e.target) && !links.contains(e.target)) {
      links.classList.remove('open');
      toggle.setAttribute('aria-expanded', 'false');
    }
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      links.classList.remove('open');
      toggle.setAttribute('aria-expanded', 'false');
    }
  });
}

// Smooth scroll for anchor links
function initSmoothScroll() {
  document.querySelectorAll('a[href^="#"]').forEach(a => {
    a.addEventListener('click', e => {
      const target = document.querySelector(a.getAttribute('href'));
      if (target) {
        e.preventDefault();
        target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    });
  });
}

// Copy install command
function initCopyButtons() {
  document.querySelectorAll('.install-cmd').forEach(el => {
    el.addEventListener('click', () => {
      const text = el.querySelector('.cmd-text')?.textContent || el.textContent.trim();
      navigator.clipboard.writeText(text).then(() => {
        const icon = el.querySelector('.copy-icon');
        if (icon) {
          icon.textContent = 'Copied!';
          setTimeout(() => { icon.textContent = 'Copy'; }, 2000);
        }
      }).catch(() => {});
    });
  });
}

// Scroll reveal animations via IntersectionObserver
function initScrollAnimations() {
  const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.classList.add('visible');
        observer.unobserve(entry.target);
      }
    });
  }, { threshold: 0.1, rootMargin: '0px 0px -50px 0px' });

  document.querySelectorAll('.reveal').forEach(el => observer.observe(el));
}

// Count-up animation for stats
function initCountUp() {
  const counters = document.querySelectorAll('.stat-value.counted');
  if (!counters.length) return;

  const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      const el = entry.target;
      const target = parseInt(el.dataset.target, 10);
      if (isNaN(target)) return;
      observer.unobserve(el);

      const duration = 1400;
      const start = performance.now();
      const format = target >= 1000;

      function tick(now) {
        const elapsed = now - start;
        const progress = Math.min(elapsed / duration, 1);
        // Ease-out cubic
        const eased = 1 - Math.pow(1 - progress, 3);
        const current = Math.round(eased * target);
        el.textContent = format ? current.toLocaleString() : current;
        if (progress < 1) requestAnimationFrame(tick);
      }
      requestAnimationFrame(tick);
    });
  }, { threshold: 0.5 });

  counters.forEach(el => observer.observe(el));
}

// Dashboard demo animation
function initDashboardDemo() {
  const demos = [
    { el: 'demoOut0', lines: ['Writing src/auth/Login.tsx...', '+47 lines in 3 files', 'Running prettier...', 'Committing changes...'], loop: true },
    { el: 'demoOut1', lines: ['Implementing token validation...', 'Reading auth/middleware.py', 'Added JWT refresh logic', 'Writing tests...'], loop: true },
    { el: 'demoOut2', lines: ['Approve test plan? [Y/n]'], cls: 'attention' },
    { el: 'demoOut3', lines: ['✓ 0 vulnerabilities found'], cls: 'success' },
  ];

  demos.forEach(({ el: id, lines, loop, cls }, cardIdx) => {
    const container = document.getElementById(id);
    if (!container) return;

    let i = 0;
    function addLine() {
      if (i >= lines.length) {
        if (loop) {
          setTimeout(() => { container.innerHTML = ''; i = 0; addLine(); }, 3000);
        }
        return;
      }
      const div = document.createElement('div');
      div.className = 'demo-line' + (cls ? ' ' + cls : (i > 0 ? ' dim' : ''));
      div.textContent = lines[i];
      container.appendChild(div);
      while (container.children.length > 2) container.removeChild(container.firstChild);
      i++;
      setTimeout(addLine, 1000 + Math.random() * 1000);
    }
    setTimeout(addLine, 1800 + cardIdx * 500);
  });
}

// Active nav link
function initActiveNav() {
  const path = window.location.pathname.replace(/\/$/, '') || '/';
  document.querySelectorAll('.nav-links a, .docs-nav-group a').forEach(a => {
    const href = a.getAttribute('href')?.replace(/\/$/, '') || '/';
    if (href === path || (path.startsWith(href) && href !== '/')) {
      a.classList.add('active');
    }
  });
}

// Init
document.addEventListener('DOMContentLoaded', () => {
  initMobileNav();
  initSmoothScroll();
  initCopyButtons();
  initScrollAnimations();
  initCountUp();
  initDashboardDemo();
  initActiveNav();
});

// Apply theme before paint
initTheme();
