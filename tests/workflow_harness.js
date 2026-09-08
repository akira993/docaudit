const path = require('node:path')
const { pathToFileURL } = require('node:url')

const scriptPath = process.argv[2]
const caseName = process.argv[3]
const calls = []
const phases = []

const runId = 'run-fixture'
const requestPath = `.claude/state/docaudit/runs/${runId}/requests/request-1.json`
const indexRoot = path.resolve('fixture-index')
const documents = [
  {
    docId: 'aaaaaaaaaaaaaaaa',
    path: 'docs/a.md',
    provenance: ['mapped'],
    contentHash: 'blob-a',
    judgementPath: `.claude/state/docaudit/runs/${runId}/requests/1/judgements/aaaaaaaaaaaaaaaa.json`,
  },
  {
    docId: 'bbbbbbbbbbbbbbbb',
    path: 'docs/b.md',
    provenance: ['self'],
    contentHash: 'blob-b',
    judgementPath: `.claude/state/docaudit/runs/${runId}/requests/1/judgements/bbbbbbbbbbbbbbbb.json`,
  },
]
const request = {
  runId: caseName === 'runid-mismatch' ? 'different-run' : runId,
  requestSeq: 1,
  attempt: 1,
  model: 'fixture-model',
  documents,
  retrieval: {
    method: 'index',
    indexDb: path.join(indexRoot, 'index.sqlite'),
    indexCwd: path.join(indexRoot, 'mirror'),
    indexLang: 'ja-jp',
    fallback: 'grep',
  },
  donePath: `.claude/state/docaudit/runs/${runId}/requests/request-1.done`,
  changed: ['src/app.py'],
  mode: 'full',
  createdAt: '2026-01-01T00:00:00Z',
}

function typeMatches(value, type) {
  if (type === 'null') return value === null
  if (type === 'array') return Array.isArray(value)
  if (type === 'integer') return Number.isInteger(value)
  if (type === 'object') return value !== null && typeof value === 'object' && !Array.isArray(value)
  return typeof value === type
}

function validate(value, schema) {
  if (schema.anyOf) return schema.anyOf.some((choice) => validate(value, choice))
  if (schema.type && !typeMatches(value, schema.type)) return false
  if (schema.const !== undefined && value !== schema.const) return false
  if (schema.enum && !schema.enum.includes(value)) return false
  if (schema.type === 'integer' && schema.minimum !== undefined && value < schema.minimum) return false
  if (schema.type === 'array') {
    if (schema.minItems !== undefined && value.length < schema.minItems) return false
    if (schema.maxItems !== undefined && value.length > schema.maxItems) return false
    if (schema.items && !value.every((item) => validate(item, schema.items))) return false
  }
  if (schema.type === 'object') {
    if ((schema.required || []).some((key) => !Object.hasOwn(value, key))) return false
    if (schema.additionalProperties === false && Object.keys(value).some((key) => !Object.hasOwn(schema.properties || {}, key))) return false
    if (Object.entries(schema.properties || {}).some(([key, child]) => Object.hasOwn(value, key) && !validate(value[key], child))) return false
  }
  return true
}

function judgement(document) {
  return {
    runId,
    requestSeq: 1,
    attempt: 1,
    docId: document.docId,
    path: document.path,
    verdict: 'PASS',
    rationale: `${document.path}:1 matches`,
    evidence: [`${document.path}:1`],
    retrievalUsed: 'index',
  }
}

globalThis.args = caseName === 'null' ? JSON.stringify({ runId, requestPath }) : { runId, requestPath }
globalThis.phase = (name) => { phases.push(name) }
globalThis.log = () => {}
globalThis.pipeline = async (items) => items
globalThis.parallel = async (thunks) => Promise.all(thunks.map((thunk) => thunk()))
globalThis.agent = async (prompt, opts) => {
  calls.push({ prompt, opts })
  let value
  if (opts.phase === 'Read') {
    value = request
  } else if (opts.phase === 'Verify') {
    const document = documents.find((item) => prompt.includes(`docId: ${item.docId}`))
    if (caseName === 'null' && document.docId === documents[1].docId) return null
    value = judgement(document)
    if (caseName === 'invalid-schema' && document.docId === documents[0].docId) value.verdict = 'MAYBE'
  } else {
    const returnedDocuments = caseName === 'null' ? [documents[0].docId] : documents.map((item) => item.docId)
    value = {
      runId,
      requestSeq: 1,
      attempt: 1,
      documents: returnedDocuments,
      invocations: { reader: 1, verifiers: 2, closer: 1 },
    }
  }
  if (!validate(value, opts.schema)) throw new Error(`schema validation failed in ${opts.phase}`)
  return value
}

;(async () => {
  try {
    const moduleUrl = `${pathToFileURL(scriptPath).href}?case=${encodeURIComponent(caseName)}`
    const loaded = await import(moduleUrl)
    process.stdout.write(JSON.stringify({ ok: true, meta: loaded.meta, phases, calls }))
  } catch (error) {
    process.stdout.write(JSON.stringify({ ok: false, error: String(error.message || error), phases, calls }))
  }
})()
