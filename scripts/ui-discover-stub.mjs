// A stand-in for both outside services Discover chat talks to, so scripts/ui-discover-chat.mjs runs
// with no network and no real model: an OpenAI-compatible /chat/completions (scripted answers) and the
// OpenReads v2 endpoints bookdoe's grounding uses (search, work, audience). Usage:
//   node scripts/ui-discover-stub.mjs <port>
// It prints "listening" when ready. Answers depend only on the prompt, so the run is repeatable.
import http from 'http';

const PORT = Number(process.argv[2] || 0);
const BOOKS = {
  'stub-tale': { title: 'Stub External Tale', authors: ['Stub Author'], year: '2020', desc: 'A grown-up novel about a lighthouse keeper and the ghost who haunts the stairs of the old light.', audience: [] },
  'stub-kids': { title: 'Stub Kids Adventure', authors: ['Kay Kidd'], year: '2018', desc: "A children's book about a fox who builds a raft, for ages 6-9.", audience: [{ provider: 'google_books', field: 'categories', value: ['Juvenile Fiction'] }] },
  'stub-adult': { title: 'Stub Adult Thriller', authors: ['Dark Writer'], year: '2015', desc: 'A violent thriller with graphic content and mature themes from the first page to the last.', audience: [{ provider: 'google_books', field: 'maturity_rating', value: 'MATURE' }] },
};
const body = req => new Promise(res => { let d = ''; req.on('data', c => d += c); req.on('end', () => res(d)); });
const json = (res, code, obj) => { res.writeHead(code, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(obj)); };
const rec = (title, author, why) => ({ title, author, why, kind_hint: 'either' });

function answer(messages) {
  const system = messages[0].content;
  const kid = system.includes('THE READER IS A CHILD');
  const last = messages[messages.length - 1].content.replace(/^<user_message>|<\/user_message>$/g, '');
  const firstTurn = messages.filter(m => m.role === 'user').length === 1;
  if (/cozy/i.test(last) && firstTurn) return { assistant_text: 'Happy to help!', follow_up: 'Ebook or audiobook? Any favourites I should know about?', recommendations: [], profile_updates: null };
  if (kid) return { assistant_text: 'Here are some stories you might like.', follow_up: null, profile_updates: null,
    recommendations: [rec('Readable Kids Book', 'E2E Author', 'A quick, friendly read.'), rec('Plain Novel', 'E2E Author', 'Hidden from kids.'), rec('Stub Kids Adventure', 'Kay Kidd', 'A brave fox and a homemade raft.'), rec('Stub Adult Thriller', 'Dark Writer', 'Not for you.'), rec('Phantom Book That Does Not Exist', 'Nobody Real', 'Invented.')] };
  return { assistant_text: 'Based on that, try these.', follow_up: null,
    recommendations: [rec('Plain Novel', 'E2E Author', 'Easy to settle into.'), rec('Stub External Tale', 'Stub Author', 'Quiet and atmospheric.'), rec('Stub Adult Thriller', 'Dark Writer', 'Fast and tense.'), rec('Phantom Book That Does Not Exist', 'Nobody Real', 'Invented.')],
    profile_updates: { genres: ['cozy stories'], remember_question: 'Shall I remember that you like cozy stories?' } };
}

http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x');
  try {
    if (req.method === 'POST' && url.pathname.endsWith('/chat/completions')) {
      const payload = JSON.parse(await body(req));
      await new Promise(r => setTimeout(r, 1500));   // long enough to see the thinking state
      return json(res, 200, { choices: [{ message: { content: JSON.stringify(answer(payload.messages)) } }] });
    }
    if (req.method === 'GET' && url.pathname === '/api/v2/search') {
      const q = (url.searchParams.get('q') || '').toLowerCase();
      const items = Object.entries(BOOKS).filter(([, b]) => q.includes(b.title.toLowerCase())).map(([id, b]) => ({ id, title: b.title, authors: b.authors, first_publish_date: b.year, cover_url: null }));
      return json(res, 200, { items });
    }
    const work = url.pathname.match(/^\/api\/v2\/works\/([^/]+)(\/audience)?$/);
    if (work && BOOKS[work[1]]) {
      const b = BOOKS[work[1]];
      if (work[2] && req.method === 'POST') return json(res, 200, { audience: b.audience, complete: true });
      if (req.method === 'GET') return json(res, 200, { id: work[1], title: b.title, description: b.desc, first_publish_date: b.year, contributors: b.authors.map(n => ({ name: n, role: 'author' })), editions: [], identifiers: [] });
    }
    json(res, 404, { detail: 'not found' });
  } catch (err) { json(res, 500, { detail: String(err) }); }
}).listen(PORT, '127.0.0.1', () => console.log('listening'));
