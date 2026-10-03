import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { Markdown } from '@/components/ui/markdown'

/**
 * Rendered, not inspected as a tree.
 *
 * The bug this replaced was invisible in the component's own output — the text was
 * all there, it was simply escaped and shown as literal `###` and `|`. Only a
 * render proves a table became a `<table>`.
 */

afterEach(cleanup)

const render_ = (md: string) => render(<Markdown>{md}</Markdown>)

describe('the shapes that were rendering as punctuation', () => {
  it('renders a heading as a heading', () => {
    render_('### Key clarification')
    expect(screen.getByRole('heading', { level: 3 }).textContent).toBe('Key clarification')
    expect(document.body.textContent).not.toContain('###')
  })

  it('renders a GFM table as a table', () => {
    render_(['| Factor | Supabase |', '|---|---|', '| Setup | Minutes |'].join('\n'))
    expect(screen.getByRole('table')).toBeTruthy()
    expect(screen.getByRole('columnheader', { name: 'Factor' })).toBeTruthy()
    expect(screen.getByRole('cell', { name: 'Minutes' })).toBeTruthy()
    expect(document.body.textContent).not.toContain('| Factor')
  })

  it('renders bold and inline code', () => {
    render_('**bold** and `mi stats`')
    expect(screen.getByText('bold').tagName).toBe('STRONG')
    expect(screen.getByText('mi stats').tagName).toBe('CODE')
  })

  it('renders fenced code as a block', () => {
    render_('```sql\nselect 1;\n```')
    const pre = document.querySelector('pre')
    expect(pre?.textContent).toContain('select 1;')
  })

  it('renders lists, rules and blockquotes', () => {
    render_(['- one', '- two', '', '---', '', '> quoted'].join('\n'))
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
    expect(document.querySelector('hr')).toBeTruthy()
    expect(document.querySelector('blockquote')?.textContent).toContain('quoted')
  })

  it('renders a link, opened safely', () => {
    render_('[docs](https://example.com/x)')
    const a = screen.getByRole('link')
    expect(a.getAttribute('href')).toBe('https://example.com/x')
    expect(a.getAttribute('target')).toBe('_blank')
    // no `window.opener` handoff to the linked page
    expect(a.getAttribute('rel')).toContain('noreferrer')
  })

  it('renders strikethrough, which is GFM-only', () => {
    render_('~~gone~~')
    expect(document.querySelector('del')?.textContent).toBe('gone')
  })
})

describe('safety', () => {
  it('does not render raw HTML from a model answer', () => {
    // The whole reason `rehype-raw` is not installed: a response must not be able
    // to inject markup into the dashboard.
    render_('<img src=x onerror=alert(1)>\n\n<script>alert(2)</script>')
    expect(document.querySelector('img')).toBeNull()
    expect(document.querySelector('script')).toBeNull()
    expect(document.body.textContent).toContain('onerror')
  })
})

describe('a partial answer mid-stream', () => {
  it('renders an unterminated fence without throwing', () => {
    expect(() => render_('here you go:\n\n```sql\nselect 1')).not.toThrow()
  })

  it('renders an unterminated table without throwing', () => {
    expect(() => render_('| a | b |\n|---|---|\n| 1 |')).not.toThrow()
  })

  it('renders a half-written bold marker without throwing', () => {
    expect(() => render_('this is **half')).not.toThrow()
  })
})

describe('long unbroken tokens', () => {
  it('wraps rather than widening the bubble past the viewport', () => {
    // A model id or URL is the realistic case, and it has no space to break on.
    const { container } = render_(`openrouter:nvidia/${'x'.repeat(120)}:free`)
    expect(container.firstElementChild?.className).toContain('overflow-wrap')
  })
})
