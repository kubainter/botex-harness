<template>
  <div class="markdown-body max-w-none text-slate-200 text-sm leading-relaxed" v-html="renderedHtml" @click="handleCopyClick"></div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { marked } from 'marked'

const props = withDefaults(
  defineProps<{
    content: string
  }>(),
  {
    content: ''
  }
)

marked.setOptions({
  gfm: true,
  breaks: true
})

const renderedHtml = computed(() => {
  if (!props.content) return ''
  try {
    let raw = marked.parse(props.content) as string
    raw = raw.replace(
      /<pre><code([^>]*)>([\s\S]*?)<\/code><\/pre>/gi,
      (_match, attrs, code) => {
        return `
          <div class="relative group my-3 rounded-lg overflow-hidden border border-botex-700 bg-botex-950 font-mono text-xs">
            <div class="flex items-center justify-between px-3 py-1.5 bg-botex-900 border-b border-botex-800 text-slate-400">
              <span class="text-xs text-slate-400 font-mono">code</span>
              <button
                type="button"
                data-copy-code="true"
                class="min-h-[44px] lg:min-h-[36px] px-2 py-0.5 rounded text-xs bg-botex-800 hover:bg-botex-700 text-slate-300 hover:text-white transition flex items-center space-x-1"
              >
                Copy
              </button>
            </div>
            <pre class="p-3 overflow-x-auto text-slate-200 font-mono text-xs leading-5"><code${attrs}>${code}</code></pre>
          </div>
        `
      }
    )
    return raw
  } catch (err) {
    return `<pre class="whitespace-pre-wrap">${props.content}</pre>`
  }
})

function handleCopyClick(event: MouseEvent) {
  const target = event.target as HTMLElement
  if (target && target.getAttribute('data-copy-code') === 'true') {
    const preWrapper = target.closest('.group')
    const codeEl = preWrapper?.querySelector('code')
    if (codeEl) {
      navigator.clipboard.writeText(codeEl.innerText).then(() => {
        const originalText = target.innerText
        target.innerText = 'Copied!'
        target.classList.add('text-emerald-400')
        setTimeout(() => {
          target.innerText = originalText
          target.classList.remove('text-emerald-400')
        }, 2000)
      })
    }
  }
}
</script>

<style>
.markdown-body h1, .markdown-body h2, .markdown-body h3, .markdown-body h4 {
  color: rgb(var(--botex-50));
  font-weight: 600;
  margin-top: 1.25rem;
  margin-bottom: 0.5rem;
}
.markdown-body h1 { font-size: 1.25rem; }
.markdown-body h2 { font-size: 1.125rem; }
.markdown-body h3 { font-size: 1rem; }
.markdown-body p { margin-bottom: 0.75rem; }
.markdown-body ul, .markdown-body ol {
  margin-left: 1.25rem;
  margin-bottom: 0.75rem;
}
.markdown-body ul { list-style-type: disc; }
.markdown-body ol { list-style-type: decimal; }
.markdown-body li { margin-bottom: 0.25rem; }
.markdown-body blockquote {
  border-left: 2px solid rgb(var(--botex-700));
  padding-left: 0.75rem;
  color: rgb(var(--botex-300));
  font-style: italic;
  margin: 0.75rem 0;
}
.markdown-body a {
  color: #0284c7;
  text-decoration: underline;
  text-underline-offset: 2px;
}
html.dark .markdown-body a {
  color: #38bdf8;
}
.markdown-body hr {
  border-color: rgb(var(--botex-700));
  margin: 1rem 0;
}
.markdown-body table {
  width: 100%;
  border-collapse: collapse;
  margin: 0.75rem 0;
  font-size: 0.8125rem;
}
.markdown-body th, .markdown-body td {
  border: 1px solid rgb(var(--botex-700));
  padding: 0.375rem 0.625rem;
}
.markdown-body th {
  background-color: rgb(var(--botex-800));
  color: rgb(var(--botex-100));
  font-weight: 600;
}
.markdown-body input[type='checkbox'] {
  width: 1rem;
  height: 1rem;
  margin-right: 0.375rem;
  vertical-align: -0.15em;
  accent-color: #0ea5e9;
}
.markdown-body li:has(> input[type='checkbox']) {
  list-style: none;
  margin-left: -1.25rem;
}
</style>
