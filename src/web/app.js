// Notebook page behaviour. Inlined into the page by src/webpage.py.
(function () {
  'use strict';

  // -- shared helpers -------------------------------------------------------

  var toasts = document.getElementById('toasts');

  // A short message in the corner; errors stay longer. Click to dismiss.
  function toast(message, kind) {
    var el = document.createElement('div');
    el.className = 'toast' + (kind ? ' ' + kind : '');
    el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    el.textContent = message;
    var close = function () { if (el.parentNode) el.parentNode.removeChild(el); };
    el.addEventListener('click', close);
    setTimeout(close, kind === 'error' ? 9000 : 4500);
    toasts.appendChild(el);
  }

  // Show a spinner on a button (its size stays the same) and block clicks.
  function busy(btn, on) {
    btn.classList.toggle('busy', on);
    btn.disabled = on;
    btn.setAttribute('aria-busy', on ? 'true' : 'false');
  }

  // POST JSON; always resolves to the server's {ok, ...} answer.
  function post(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().catch(function () {
        return { ok: false, error: 'server answered ' + r.status };
      });
    }).catch(function () {
      return { ok: false, error: 'could not reach the server' };
    });
  }

  // Busy button -> request -> onOk(result), or a toast with the reason.
  function run(btn, url, body, onOk, failure) {
    busy(btn, true);
    return post(url, body).then(function (res) {
      if (res.ok) { onOk(res); return; }
      busy(btn, false);
      toast(failure + ': ' + (res.error || 'unknown error'), 'error');
    });
  }

  function reload() { window.location.reload(); }

  function kindName(kind) { return kind === 'session' ? 'recording' : 'page'; }

  function refreshCounts() {
    var pages = document.getElementById('n-pages');
    var sessions = document.getElementById('n-sessions');
    if (pages) pages.textContent = document.querySelectorAll('.card.page').length;
    if (sessions) sessions.textContent = document.querySelectorAll('.card.sess').length;
    document.dispatchEvent(new Event('echo:filtered'));
  }

  // Fade a deleted card out, then remove it.
  function removeCard(card) {
    card.classList.add('leaving');
    setTimeout(function () {
      if (card.parentNode) card.parentNode.removeChild(card);
      refreshCounts();
    }, 260);
  }

  // -- per-card actions -------------------------------------------------------

  function deleteOne(btn) {
    var kind = btn.getAttribute('data-kind');
    var id = btn.getAttribute('data-id');
    if (!confirm('Delete this ' + kindName(kind) + ' (' + id + ')? This cannot be undone.')) return;
    run(btn, '/api/delete/' + kind, { id: id }, function () {
      removeCard(btn.closest('.card'));
      toast('Deleted ' + kindName(kind) + ' ' + id, 'ok');
    }, 'Delete failed');
  }

  function renameNotebook(btn) {
    var id = btn.getAttribute('data-nb');
    var cur = btn.getAttribute('data-cur') || id;
    var name = prompt('Rename notebook ' + cur + ' to:', cur);
    if (name === null) return;
    name = name.trim();
    if (!name && !confirm('Clear the notebook name and show the raw id again?')) return;
    run(btn, '/api/rename', { id: id, name: name }, reload, 'Rename failed');
  }

  function retitle(btn) {
    var id = btn.getAttribute('data-id');
    var cur = btn.getAttribute('data-cur') || '';
    var what = /^PRS-/.test(id) ? 'recording' : 'page';
    var ask = cur ? 'Change the title of ' + what + ' ' + id + ' to:'
                  : 'Give ' + what + ' ' + id + ' a title:';
    var name = prompt(ask, cur);
    if (name === null) return;
    name = name.trim();
    if (!name && !cur) return;
    if (!name && !confirm('Clear the title?')) return;
    run(btn, '/api/title', { id: id, name: name }, reload, 'Saving the title failed');
  }

  function transcribe(btn) {
    var kind = btn.getAttribute('data-kind');
    var id = btn.getAttribute('data-id');
    if (!confirm('Transcribe this ' + kindName(kind) + ' (' + id + ')? This may take a little while.')) return;
    toast('Transcribing ' + kindName(kind) + ' ' + id + '…');
    run(btn, '/api/transcribe/' + kind, { id: id }, reload, 'Transcription failed');
  }

  function pushNote(btn) {
    var kind = btn.getAttribute('data-kind');
    var id = btn.getAttribute('data-id');
    run(btn, '/api/push/' + kind, { id: id }, function (res) {
      busy(btn, false);
      toast(res.message || 'Sent to Hermes', 'ok');
    }, 'Sending to Hermes failed');
  }

  // The bulk "Delete selected" button is also .delete but has no data-id.
  var ACTIONS = [
    ['.delete[data-id]', deleteOne],
    ['.rename', renameNotebook],
    ['.titleedit', retitle],
    ['.transcribe', transcribe],
    ['.push', pushNote]
  ];

  document.addEventListener('click', function (e) {
    if (!e.target.closest) return;
    for (var i = 0; i < ACTIONS.length; i++) {
      var btn = e.target.closest(ACTIONS[i][0]);
      if (btn) { e.preventDefault(); ACTIONS[i][1](btn); return; }
    }
  });

  // -- search -------------------------------------------------------------------

  function setUpSearch() {
    var q = document.getElementById('q');
    var count = document.getElementById('count');
    if (!q) return;
    function apply() {
      var cards = document.querySelectorAll('.card.page, .card.sess');
      var term = q.value.trim().toLowerCase();
      var shown = 0;
      Array.prototype.forEach.call(cards, function (card) {
        var haystack = card.getAttribute('data-search') || '';
        card.hidden = term !== '' && haystack.indexOf(term) === -1;
        if (!card.hidden) shown++;
      });
      var none = term !== '' && shown === 0;
      count.textContent = term ? (none ? 'no matches' : shown + ' of ' + cards.length) : '';
      count.classList.toggle('none', none);
      Array.prototype.forEach.call(document.querySelectorAll('.nomatch'), function (note) {
        var section = note.previousElementSibling;
        note.hidden = !section || !!section.querySelector('.card:not([hidden])');
      });
      document.dispatchEvent(new Event('echo:filtered'));
    }
    q.addEventListener('input', apply);
    apply();
  }

  // -- multi-select delete ------------------------------------------------------

  function setUpSelection() {
    var selAll = document.getElementById('sel-all');
    var selClear = document.getElementById('sel-clear');
    var selDel = document.getElementById('sel-del');
    var selCount = document.getElementById('sel-count');
    if (!selAll || !selDel) return;

    function boxes() {
      return Array.prototype.slice.call(document.querySelectorAll('.pickbox'));
    }
    function selected() {
      return boxes().filter(function (b) { return b.checked && !b.closest('[hidden]'); });
    }
    function mark(b) { b.closest('.card').classList.toggle('selected', b.checked); }
    function refresh() {
      var n = selected().length;
      selCount.textContent = n;
      // stays disabled while a delete is in flight, so it can't be sent twice
      selDel.disabled = n === 0 || selDel.classList.contains('busy');
    }

    document.addEventListener('echo:filtered', refresh);
    document.addEventListener('change', function (e) {
      if (!e.target.classList.contains('pickbox')) return;
      mark(e.target);
      refresh();
    });
    selAll.addEventListener('change', function () {
      boxes().forEach(function (b) {
        if (!b.closest('[hidden]')) b.checked = selAll.checked;
        mark(b);
      });
      refresh();
    });
    selClear.addEventListener('click', function () {
      boxes().forEach(function (b) { b.checked = false; mark(b); });
      selAll.checked = false;
      refresh();
    });
    selDel.addEventListener('click', function () {
      var items = selected().map(function (b) {
        return { kind: b.getAttribute('data-kind'), id: b.getAttribute('data-id') };
      });
      if (!items.length) return;
      var body = { items: items };
      if (items.length > 5) {
        // matches BULK_DELETE_LIMIT in serve.py: big selections need a
        // typed confirmation so the library can't be wiped by a slip
        var typed = prompt('You selected ' + items.length + ' items.\n' +
          'This permanently deletes them from the notebook AND the raw pen\n' +
          'data - it cannot be undone.\n\nType DELETE (all caps) to proceed:');
        if (typed !== 'DELETE') return;
        body.confirm = 'DELETE';
      } else if (!confirm('Delete ' + items.length + ' selected item(s)? This cannot be undone.')) {
        return;
      }
      run(selDel, '/api/delete', body, reload, 'Delete failed');
    });
    refresh();
  }

  setUpSearch();
  setUpSelection();
})();
