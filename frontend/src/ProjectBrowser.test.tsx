import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ProjectBrowser } from './ProjectBrowser'
import type { ProjectSummary } from './contracts'

const summary: ProjectSummary = {
  project: {
    id: 'project_wager',
    name: 'The Wager',
    description: '',
    active_revision_id: 'revision_1',
    preferences: {},
    created_at: '2026-07-16T00:00:00Z',
    updated_at: '2026-07-17T00:00:00Z',
    archived_at: null,
  },
  thumbnail_url: '/api/projects/project_wager/artifacts/artifact_preview',
  thumbnail_kind: 'preview',
  source_filename: 'wager.png',
  source_width_px: 1800,
  source_height_px: 1200,
  canvas_width_mm: 200,
  canvas_height_mm: 200,
  color_count: 4,
  current_revision_id: 'revision_1',
  current_revision_label: 'Ready to print',
  status: 'preview_ready',
  validation: 'validated',
}

describe('ProjectBrowser', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        if (url === '/api/projects?include_archived=true') {
          return new Response(JSON.stringify({ items: [summary], total: 1 }), {
            status: 200,
          })
        }
        if (url.endsWith('/archive') && init?.method === 'POST') {
          return new Response(
            JSON.stringify({
              ...summary,
              project: {
                ...summary.project,
                archived_at: '2026-07-17T01:00:00Z',
              },
              status: 'archived',
            }),
            { status: 200 },
          )
        }
        if (url.endsWith('/restore') && init?.method === 'POST') {
          return new Response(JSON.stringify(summary), { status: 200 })
        }
        if (url.includes('/api/projects/project_wager/bundle?')) {
          return new Response(new TextEncoder().encode('bundle'), {
            status: 200,
            headers: {
              'Content-Disposition':
                'attachment; filename="the-wager.image23mf"',
              'X-Image23MF-Bundle-SHA256': 'a'.repeat(64),
              'X-Image23MF-History-Mode': 'full_history',
            },
          })
        }
        if (url === '/api/project-bundles/import' && init?.method === 'POST') {
          return new Response(
            JSON.stringify({
              project_id: 'project_imported',
              source_project_id: 'project_wager',
              bundle_sha256: 'b'.repeat(64),
              duplicate: false,
              imported_assets: 1,
              imported_revisions: 1,
              imported_artifacts: 2,
              history_mode: 'full_history',
              history_import: 'full_history',
              imported_history_nodes: 4,
              abandoned_history_nodes: 1,
            }),
            { status: 201, headers: { 'Content-Type': 'application/json' } },
          )
        }
        return new Response(null, { status: 404 })
      }),
    )
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('supports search, keyboard open, archive, and restore while preserving thumbnail evidence', async () => {
    const onOpen = vi.fn()
    const { container } = render(<ProjectBrowser onOpen={onOpen} />)

    const search = await screen.findByRole('searchbox', {
      name: 'Search projects',
    })
    expect(search).toHaveFocus()
    expect(
      container.querySelector('img[data-thumbnail-kind="preview"]'),
    ).toHaveAttribute('src', summary.thumbnail_url)
    expect(screen.getByText('200 × 200 mm')).toBeInTheDocument()
    expect(screen.getByText('4')).toBeInTheDocument()
    expect(screen.getByText('Ready to print')).toBeInTheDocument()
    expect(screen.getByText('Validated')).toBeInTheDocument()

    fireEvent.change(search, { target: { value: 'missing' } })
    expect(
      screen.getByText('No projects match that search.'),
    ).toBeInTheDocument()
    fireEvent.change(search, { target: { value: 'wager.png' } })

    const open = screen.getByRole('button', { name: 'Open project' })
    open.focus()
    fireEvent.click(open)
    expect(onOpen).toHaveBeenCalledWith('project_wager')

    fireEvent.change(search, { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Archive' }))
    await waitFor(() =>
      expect(screen.getByText('No saved projects yet.')).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByRole('checkbox', { name: 'Show archived' }))
    expect(await screen.findByText('Archived')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Restore' }))
    await waitFor(() =>
      expect(screen.getByText('No archived projects.')).toBeInTheDocument(),
    )
  })

  it('renders a useful first-project empty state', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ items: [], total: 0 }), { status: 200 }),
    )
    render(<ProjectBrowser onOpen={vi.fn()} />)
    expect(
      await screen.findByText('No saved projects yet.'),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/create your first local project/i),
    ).toBeInTheDocument()
  })

  it('exports full history and imports a project bundle into the editor', async () => {
    const onOpen = vi.fn()
    const createObjectURL = vi
      .spyOn(URL, 'createObjectURL')
      .mockReturnValue('blob:bundle')
    const revokeObjectURL = vi
      .spyOn(URL, 'revokeObjectURL')
      .mockImplementation(() => undefined)
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => {})
    const { container } = render(<ProjectBrowser onOpen={onOpen} />)
    await screen.findByText('The Wager')

    fireEvent.click(
      screen.getByRole('button', {
        name: 'Export The Wager with full history',
      }),
    )
    expect(
      await screen.findByText(
        'Exported The Wager with full Undo/Redo history.',
      ),
    ).toBeInTheDocument()
    expect(createObjectURL).toHaveBeenCalledOnce()
    expect(anchorClick).toHaveBeenCalledOnce()
    await waitFor(() =>
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:bundle'),
    )

    fireEvent.change(screen.getByLabelText('Choose project bundle'), {
      target: {
        files: [
          new File(['bundle'], 'the-wager.image23mf', {
            type: 'application/zip',
          }),
        ],
      },
    })
    const dialog = await screen.findByRole('dialog', {
      name: 'Project bundle is ready',
    })
    expect(dialog).toHaveTextContent('Full history')
    expect(dialog).toHaveTextContent('4')
    expect(dialog).toHaveTextContent('1')
    expect(dialog).toHaveTextContent('New project copy')
    const keepBrowsing = screen.getByRole('button', { name: 'Keep browsing' })
    const openImported = screen.getByRole('button', {
      name: 'Open imported project',
    })
    await waitFor(() => expect(keepBrowsing).toHaveFocus())
    expect(container.querySelector('.project-browser')).toHaveAttribute('inert')
    expect(document.body.style.overflow).toBe('hidden')
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
    expect(openImported).toHaveFocus()
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(keepBrowsing).toHaveFocus()
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() =>
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument(),
    )
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: 'Import project bundle' }),
      ).toHaveFocus(),
    )
    expect(container.querySelector('.project-browser')).not.toHaveAttribute(
      'inert',
    )
    expect(document.body.style.overflow).toBe('')

    fireEvent.change(screen.getByLabelText('Choose project bundle'), {
      target: {
        files: [
          new File(['bundle'], 'the-wager.image23mf', {
            type: 'application/zip',
          }),
        ],
      },
    })
    await screen.findByRole('dialog', { name: 'Project bundle is ready' })
    expect(onOpen).not.toHaveBeenCalled()
    fireEvent.click(
      screen.getByRole('button', { name: 'Open imported project' }),
    )
    expect(onOpen).toHaveBeenCalledWith('project_imported')
    expect(fetch).toHaveBeenCalledWith(
      '/api/project-bundles/import',
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('sorts populated projects by recent activity or name', async () => {
    const alpha: ProjectSummary = {
      ...summary,
      project: {
        ...summary.project,
        id: 'project_alpha',
        name: 'Alpha study',
        updated_at: '2026-07-15T00:00:00Z',
      },
      thumbnail_url: '/api/assets/alpha',
      thumbnail_kind: 'source',
      source_filename: 'alpha.png',
    }
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ items: [alpha, summary], total: 2 }), {
        status: 200,
      }),
    )
    const { container } = render(<ProjectBrowser onOpen={vi.fn()} />)
    await screen.findByText('Alpha study')
    const names = () =>
      [...container.querySelectorAll('.project-card h2')].map(
        (node) => node.textContent,
      )
    expect(names()).toEqual(['The Wager', 'Alpha study'])
    fireEvent.change(screen.getByRole('combobox', { name: 'Sort' }), {
      target: { value: 'name_asc' },
    })
    expect(names()).toEqual(['Alpha study', 'The Wager'])
  })
})
