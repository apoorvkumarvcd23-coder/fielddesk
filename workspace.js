/* Identity tokens stay in closure memory. Untrusted text is never inserted as HTML. */
(() => {
  const el = id => document.getElementById(id);
  let token = '', actor = null, epoch = 0, controller = new AbortController(), handoff = null;
  const text = (id, value) => { el(id).textContent = value; };
  const node = (tag, value, className) => {
    const result = document.createElement(tag); result.textContent = value;
    if (className) result.className = className; return result;
  };
  async function api(path, body, method) {
    const generation = epoch;
    const response = await fetch(path, { method: method || (body === undefined ? 'GET' : 'POST'),
      signal: controller.signal, cache: 'no-store', credentials: 'omit',
      headers: { Authorization: 'Bearer ' + token, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    if (generation !== epoch) throw new Error('Identity changed; retry in the current workspace.');
    const result = await response.json();
    if (generation !== epoch) throw new Error('Identity changed; retry in the current workspace.');
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Request validation failed. Check the form.');
    return result;
  }
  async function perform(button, work) {
    const generation = epoch; button.disabled = true; text('error', '');
    try { await work(generation); }
    catch (failure) { if (generation === epoch && failure.name !== 'AbortError') text('error', failure.message || 'Service unavailable.'); }
    finally { if (generation === epoch) button.disabled = false; }
  }
  function clear() {
    controller.abort(); controller = new AbortController(); epoch++; token = ''; actor = null; handoff = null;
    document.querySelectorAll('button').forEach(button => { button.disabled = false; });
    for (const id of ['identity','configuration','answer','answer-status','citations','reviews','jobs','error','review-status','ingest-status','grant-status','space','grant-identity']) el(id).replaceChildren();
    for (const id of ['workspace','connected','operator-tools','admin-tools','handoff']) el(id).hidden = true;
    el('connect-form').hidden = false; el('token').value = ''; el('question').value = '';
    for (const id of ['source-key','doc-title','doc-body','grant-document']) el(id).value = '';
    el('consent').checked = false; el('request-review').disabled = true;
    el('grant-enabled').checked = true;
  }
  async function refreshReviews() {
    const rows = await api('/rag/reviews'); const box = el('reviews'); box.replaceChildren();
    if (!rows.length) box.append(node('p','No review requests yet.'));
    for (const row of rows) renderReview(box, row);
  }
  async function refreshJobs() {
    const rows = await api('/rag/jobs'); const box = el('jobs'); box.replaceChildren();
    if (!rows.length) box.append(node('p','No permitted indexing jobs.'));
    for (const job of rows) {
      const item = node('article', '', 'item');
      item.append(node('strong', job.status.toUpperCase()), node('p', job.id, 'muted'),
        node('p', job.indexed_chunks + ' indexed chunks · attempt ' + job.attempts + '/' + job.max_attempts + (job.last_error ? ' · ' + job.last_error : '')));
      box.append(item);
    }
  }
  function button(parent, label, handler, secondary = false) {
    const b = node('button', label, secondary ? 'secondary' : '');
    b.type = 'button'; b.onclick = () => perform(b, handler); parent.append(b); return b;
  }
  function renderReview(box, row) {
    const item = node('article', '', 'item'); item.dataset.reviewId = row.id;
    item.append(node('span', row.state.replace('_',' '), 'badge'), node('p', row.question || 'Shared question text has been withdrawn or expired.'),
      node('p', 'Requester: ' + row.requester + ' · revision ' + row.revision, 'muted'));
    const privileged = actor.role === 'operator' || actor.role === 'admin';
    if (privileged && row.state === 'open' && row.deleted_at === null)
      button(item, 'Claim review', async () => { await api('/rag/reviews/' + row.id + '/claim', { expected_revision: row.revision }); await refreshReviews(); text('review-status','Review claimed.'); });
    if (privileged && row.state === 'in_review' && row.assigned_to === actor.id) {
      const label = node('label', 'Human ticket title'); const title = node('input','');
      title.maxLength = 300; title.value = row.action ? row.action.title : ''; label.append(title); item.append(label);
      button(item, row.action ? 'Refresh ticket proposal' : 'Propose service ticket', async () => {
        if (!title.value.trim()) throw new Error('Enter a ticket title.');
        await api('/rag/reviews/' + row.id + '/proposal', { expected_revision: row.revision, title: title.value.trim() });
        await refreshReviews(); text('review-status','Ticket proposed. Explicit approval is still required.');
      });
      if (row.action && row.action.status === 'pending') button(item, 'Approve service ticket', async () => {
        const saved = await api('/rag/reviews/' + row.id + '/approve', { expected_revision: row.revision });
        await refreshReviews(); text('review-status','Approved ticket: ' + saved.action.ticket_id);
      });
      const resolutionLabel = node('label', 'Resolution note'); const resolution = node('input','');
      resolution.maxLength = 600; resolutionLabel.append(resolution); item.append(resolutionLabel);
      button(item, 'Resolve without ticket', async () => {
        if (!resolution.value.trim()) throw new Error('Enter a resolution note.');
        await api('/rag/reviews/' + row.id + '/resolve', { expected_revision: row.revision, resolution: resolution.value.trim() });
        await refreshReviews(); text('review-status','Review resolved without creating a ticket.');
      }, true);
    }
    if (row.action) item.append(node('p', 'Ticket proposal: ' + row.action.title + ' · ' + row.action.status));
    if (row.action && row.action.ticket_id) button(item, 'View service ticket', async () => {
      const ticket = await api('/rag/tickets/' + row.action.ticket_id);
      item.append(node('p', 'Ticket ' + ticket.id + ': ' + ticket.title + ' — ' + ticket.status));
    }, true);
    if (row.resolution) item.append(node('p', row.resolution));
    if (row.events.length) item.append(node('p', 'History: ' + row.events.map(e => e.event.replaceAll('_',' ')).join(' → '), 'muted'));
    if (row.requester_id === actor.id && row.question) button(item, 'Withdraw shared question', async () => {
      await api('/rag/reviews/' + row.id, undefined, 'DELETE'); await refreshReviews(); text('review-status','Shared question text withdrawn. Existing approved tickets remain.');
    }, true);
    box.append(item);
  }
  el('connect-form').onsubmit = event => {
    event.preventDefault(); const supplied = el('token').value; clear(); token = supplied;
    perform(el('connect'), async () => {
      actor = await api('/rag/me'); const config = await api('/rag/config');
      text('identity', 'Connected as ' + actor.name + ' · ' + actor.role);
      text('configuration', config.storage + ' · ' + (config.embedding ? config.embedding.model : 'No embedding model configured') + ' · ' + (config.generation_model || 'Generation unavailable'));
      el('connect-form').hidden = true; el('connected').hidden = false; el('workspace').hidden = false;
      el('question').value = 'What must I record in a projector repair ticket?';
      const privileged = actor.role !== 'user';
      text('review-heading', privileged ? 'Operator review queue' : 'Your review requests');
      el('operator-tools').hidden = !privileged; el('admin-tools').hidden = actor.role !== 'admin';
      await refreshReviews();
      if (privileged) {
        const spaces = await api('/rag/spaces');
        for (const space of spaces.filter(s => s.backend === 'ollama')) {
          const option = node('option', space.model + ' · ' + space.dimensions + ' dimensions'); option.value = space.id; el('space').append(option);
        }
        el('ingest').disabled = !el('space').value;
        await refreshJobs();
      }
      if (actor.role === 'admin') for (const identity of await api('/rag/identities')) {
        if (!identity.enabled) continue;
        const option = node('option', identity.name + ' · ' + identity.role); option.value = identity.id; el('grant-identity').append(option);
      }
    });
  };
  el('disconnect').onclick = clear;
  el('question').oninput = () => { handoff = null; el('handoff').hidden = true; el('consent').checked = false; el('request-review').disabled = true; };
  el('ask-form').onsubmit = event => {
    event.preventDefault(); handoff = null; el('handoff').hidden = true; el('consent').checked = false; text('answer',''); el('citations').replaceChildren();
    perform(el('ask'), async () => {
      const question = el('question').value.trim(); text('answer-status','Checking permitted notes…');
      const result = await api('/rag/chat', { message: question }); text('answer', result.answer);
      if (el('question').value.trim() !== question) { text('answer',''); text('answer-status','Question changed. Submit the current question again.'); return; }
      text('answer-status', result.handoff ? 'Operator handoff · ' + result.reason : 'Cited local-model answer');
      for (const cite of result.citations) {
        const item = node('div','', 'citation'); item.append(node('strong',cite.title), node('p',cite.quote));
        button(item, 'View permitted source', async () => {
          const source = await api('/rag/chunks/' + encodeURIComponent(cite.chunk_id) + '?space_id=' + encodeURIComponent(cite.space_id));
          item.append(node('p', source.text));
        }, true);
        el('citations').append(item);
      }
      if (result.handoff && el('question').value.trim() === question) { handoff = { trace_id: result.trace_id, message: question }; el('handoff').hidden = false; }
    });
  };
  el('consent').onchange = () => { el('request-review').disabled = !el('consent').checked || !handoff; };
  el('request-review').onclick = () => perform(el('request-review'), async () => {
    if (!handoff || !el('consent').checked) throw new Error('Share only a matching handoff question with explicit consent.');
    await api('/rag/reviews', { ...handoff, consent: true }); await refreshReviews(); text('review-status','Review submitted. No ticket has been created.');
  });
  el('refresh-reviews').onclick = () => perform(el('refresh-reviews'), refreshReviews);
  el('refresh-jobs').onclick = () => perform(el('refresh-jobs'), refreshJobs);
  el('ingest-form').onsubmit = event => {
    event.preventDefault(); perform(el('ingest'), async () => {
      const job = await api('/rag/documents', { source_key: el('source-key').value, title: el('doc-title').value,
        body: el('doc-body').value, space_id: el('space').value });
      text('ingest-status', 'Queued job ' + job.id + ' · ' + job.status); el('grant-document').value = job.document_id; await refreshJobs();
    });
  };
  el('grant-form').onsubmit = event => {
    event.preventDefault(); perform(event.submitter, async () => {
      await api('/rag/documents/' + encodeURIComponent(el('grant-document').value) + '/grants/' + encodeURIComponent(el('grant-identity').value),
        { enabled: el('grant-enabled').checked }, 'PUT'); text('grant-status','Document grant saved.');
    });
  };
})();
