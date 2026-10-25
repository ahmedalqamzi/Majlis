// Landing page: keeps the simulation flag on its links so a simulated session never talks to a real server.
import { mount, simRibbon, pageUrl } from './ui.js';

const simEl = document.getElementById('sim');
if (simEl) mount(simEl, simRibbon());
for (const [id, path] of [['go-play', 'play.html'], ['go-host', 'host.html']]) {
  const a = document.getElementById(id);
  if (a) a.href = pageUrl(path);
}
