(function () {
  'use strict';
  var root = document.getElementById('uploader');
  if (!root) return;
  var csrf = root.getAttribute('data-csrf');
  var input = document.getElementById('files');
  var drop = document.querySelector('.dropzone');
  var queue = document.getElementById('queue');
  var send = document.getElementById('send');
  var clientSelect = document.querySelector('select[name="client_id"]');
  var files = [];
  function render() {
    queue.innerHTML = '';
    files.forEach(function (f, i) {
      var li = document.createElement('li');
      li.textContent = f.name + ' (' + Math.round(f.size / 1024) + ' KB)' + (f.status ? ' — ' + f.status : '');
      queue.appendChild(li);
    });
    send.disabled = files.length === 0 || !clientSelect.value;
  }
  function add(list) {
    Array.prototype.forEach.call(list, function (f) { files.push(f); });
    render();
  }
  input.addEventListener('change', function () { add(input.files); });
  drop.addEventListener('click', function () { input.click(); });
  drop.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } });
  drop.addEventListener('dragover', function (e) { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', function () { drop.classList.remove('over'); });
  drop.addEventListener('drop', function (e) { e.preventDefault(); drop.classList.remove('over'); add(e.dataTransfer.files); });
  clientSelect.addEventListener('change', render);
  send.addEventListener('click', async function () {
    send.disabled = true;
    for (var i = 0; i < files.length; i++) {
      var f = files[i];
      f.status = 'uploading…'; render();
      try {
        var res = await fetch('/invoices/upload?client_id=' + encodeURIComponent(clientSelect.value), {
          method: 'POST', credentials: 'same-origin',
          headers: { 'X-CSRF-Token': csrf, 'X-Filename': encodeURIComponent(f.name), 'Content-Type': 'application/octet-stream' },
          body: f
        });
        var data = await res.json().catch(function () { return {}; });
        if (res.ok) {
          f.status = 'processed: ' + data.status.replace(/_/g, ' ') + (data.extraction_status ? ' (' + data.extraction_status + ')' : '') + ' — ';
          var a = document.createElement('a'); a.href = data.review_url; a.textContent = 'review';
          f.link = a;
        } else {
          f.status = 'rejected: ' + (data.error || res.statusText);
        }
      } catch (err) {
        f.status = 'network error';
      }
      render();
      if (f.link) { var li = queue.children[i]; li.appendChild(f.link); }
    }
    files = files.filter(function (f) { return !f.link; });
    send.disabled = false;
  });
})();
