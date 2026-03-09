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
    { el: 'demoOut3', lines: ['\u2713 0 vulnerabilities found'], cls: 'success' },
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

// Hero particle constellation
function initHeroCanvas() {
  const canvas = document.getElementById('heroCanvas');
  if (!canvas) return;

  const ctx = canvas.getContext('2d');
  let w, h, particles, animId;
  const COLORS = ['#706CF0', '#8B87F5', '#5854c7', '#3B82F6', '#8B5CF6', '#22C55E', '#F59E0B', '#EF4444'];
  const MAX_DIST = 180;
  const PARTICLE_COUNT = () => Math.min(Math.floor((w * h) / 18000), 80);

  function resize() {
    const rect = canvas.parentElement.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    w = rect.width;
    h = rect.height;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function createParticle() {
    const color = COLORS[Math.floor(Math.random() * COLORS.length)];
    return {
      x: Math.random() * w,
      y: Math.random() * h,
      vx: (Math.random() - 0.5) * 0.4,
      vy: (Math.random() - 0.5) * 0.4,
      r: Math.random() * 2 + 1,
      color,
      alpha: Math.random() * 0.5 + 0.3,
      pulsePhase: Math.random() * Math.PI * 2,
    };
  }

  function initParticles() {
    particles = [];
    const count = PARTICLE_COUNT();
    for (let i = 0; i < count; i++) particles.push(createParticle());
  }

  function drawPulse(x1, y1, x2, y2, t, color) {
    const progress = (t % 3000) / 3000;
    const px = x1 + (x2 - x1) * progress;
    const py = y1 + (y2 - y1) * progress;
    ctx.beginPath();
    ctx.arc(px, py, 2, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.globalAlpha = 0.6;
    ctx.fill();
    ctx.globalAlpha = 1;
  }

  let time = 0;
  function draw() {
    time += 16;
    ctx.clearRect(0, 0, w, h);

    const isLight = document.documentElement.getAttribute('data-theme') === 'light';
    const globalDim = isLight ? 0.4 : 1;

    // Update positions
    for (const p of particles) {
      p.x += p.vx;
      p.y += p.vy;
      if (p.x < -20) p.x = w + 20;
      if (p.x > w + 20) p.x = -20;
      if (p.y < -20) p.y = h + 20;
      if (p.y > h + 20) p.y = -20;
    }

    // Draw connections
    for (let i = 0; i < particles.length; i++) {
      for (let j = i + 1; j < particles.length; j++) {
        const a = particles[i], b = particles[j];
        const dx = a.x - b.x, dy = a.y - b.y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < MAX_DIST) {
          const opacity = (1 - dist / MAX_DIST) * 0.15 * globalDim;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.strokeStyle = a.color;
          ctx.globalAlpha = opacity;
          ctx.lineWidth = 0.5;
          ctx.stroke();
          ctx.globalAlpha = 1;

          // Occasional pulse traveling along connection
          if (dist < MAX_DIST * 0.6 && (i + j) % 7 === 0) {
            drawPulse(a.x, a.y, b.x, b.y, time + i * 500, a.color);
          }
        }
      }
    }

    // Draw particles
    for (const p of particles) {
      const pulse = Math.sin(time * 0.002 + p.pulsePhase) * 0.3 + 0.7;
      const alpha = p.alpha * pulse * globalDim;

      // Glow
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r * 4, 0, Math.PI * 2);
      ctx.fillStyle = p.color;
      ctx.globalAlpha = alpha * 0.1;
      ctx.fill();

      // Core
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fillStyle = p.color;
      ctx.globalAlpha = alpha;
      ctx.fill();
      ctx.globalAlpha = 1;
    }

    animId = requestAnimationFrame(draw);
  }

  // Intersection observer — only animate when visible
  const observer = new IntersectionObserver(([entry]) => {
    if (entry.isIntersecting) {
      if (!animId) draw();
    } else {
      if (animId) { cancelAnimationFrame(animId); animId = null; }
    }
  }, { threshold: 0 });

  resize();
  initParticles();
  observer.observe(canvas);

  let resizeTimer;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { resize(); initParticles(); }, 200);
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
  initHeroCanvas();
  initActiveNav();
});

// Apply theme before paint
initTheme();
