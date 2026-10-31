'use strict';
const message = new URLSearchParams(location.search).get('error');
if (message) {
  const region = document.getElementById('error');
  region.textContent = message.slice(0, 400);
  region.hidden = false;
}
