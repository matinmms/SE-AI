import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

const [, , inputPath, outputPath] = process.argv;

if (!inputPath || !outputPath) {
  console.error('Usage: node render_note_pdf.mjs <input-html> <output-pdf>');
  process.exit(1);
}

const require = createRequire(import.meta.url);
const playwrightSpecifier = process.env.PLAYWRIGHT_MODULE_PATH || 'playwright';
const { chromium } = require(playwrightSpecifier);
const browserExecutablePath = process.env.BROWSER_EXECUTABLE_PATH || '';

const html = fs.readFileSync(inputPath, 'utf8');
fs.mkdirSync(path.dirname(outputPath), { recursive: true });

const browser = await chromium.launch(
  browserExecutablePath
    ? { headless: true, executablePath: browserExecutablePath }
    : { headless: true }
);

try {
  const page = await browser.newPage({
    viewport: { width: 1400, height: 1980 },
    deviceScaleFactor: 1,
  });

  await page.setContent(html, { waitUntil: 'domcontentloaded' });

  try {
    await page.waitForLoadState('networkidle', { timeout: 4000 });
  } catch {
    // Continue even if remote assets such as webfonts or MathJax are still resolving.
  }

  try {
    await page.evaluate(async () => {
      if ('fonts' in document) {
        await document.fonts.ready;
      }
    });
  } catch {
    // Ignore font readiness failures and continue with fallback fonts.
  }

  await page.emulateMedia({ media: 'print' });

  await page.evaluate(async () => {
    if (typeof window.__tesanotePreparePagination !== 'function') {
      throw new Error('The note template does not expose its pagination API.');
    }
    await window.__tesanotePreparePagination({ force: true });
  });
  await page.waitForFunction(() => window.__tesanotePaginationReady === true, { timeout: 15000 });
  await page.waitForTimeout(250);

  const paginationAudit = await page.evaluate(() => {
    if (typeof window.__tesanoteConsolidateSummaryBoxes === 'function') {
      window.__tesanoteConsolidateSummaryBoxes();
    }
    if (typeof window.__tesanoteRenumberPages === 'function') {
      window.__tesanoteRenumberPages();
    } else {
      // Backward-compatible repair for an existing stored HTML artifact created
      // before the serialized pagination API was introduced.
      const pages = Array.from(document.querySelectorAll('.a4-page'));
      pages.forEach((notePage, index) => {
        const pageNumber = notePage.querySelector('.page-footer__page-number');
        if (!pageNumber) return;
        const label = pageNumber.dataset.pageLabel || (document.documentElement.dir === 'rtl' ? 'صفحه' : 'Page');
        const value = String(index + 1);
        pageNumber.dataset.pageIndex = value;
        pageNumber.setAttribute('aria-label', `${label} ${value}`);
        const labelNode = pageNumber.querySelector('.page-footer__page-label');
        const indexNode = pageNumber.querySelector('.page-footer__page-index');
        if (labelNode && indexNode) {
          labelNode.textContent = label;
          indexNode.textContent = value;
        } else {
          pageNumber.textContent = `${label} ${value}`;
        }
      });
    }

    const pages = Array.from(document.querySelectorAll('.a4-page'));
    const invalidPages = [];
    pages.forEach((notePage, index) => {
      const expected = String(index + 1);
      const pageNumber = notePage.querySelector('.page-footer__page-number');
      const indexNode = pageNumber?.querySelector('.page-footer__page-index');
      const rendered = (indexNode?.textContent || pageNumber?.dataset.pageIndex || '').trim();
      if (!pageNumber || rendered !== expected || pageNumber.dataset.pageIndex !== expected) {
        invalidPages.push({ page: index + 1, rendered });
      }
    });
    const summaryBoxes = Array.from(document.querySelectorAll('.summary-box'));
    const summaryBlocks = summaryBoxes.map(
      (summary) => summary.closest('.note-block') || summary
    );
    const finalContent = pages.at(-1)?.querySelector('.page-content');
    const finalBlock = finalContent?.lastElementChild;
    const summaryIsFinalBlock = summaryBlocks.length === 0 || (
      summaryBlocks.length === 1 && summaryBlocks[0] === finalBlock
    );
    return {
      pageCount: pages.length,
      invalidPages,
      summaryCount: summaryBlocks.length,
      summaryIsFinalBlock,
    };
  });

  if (
    !paginationAudit.pageCount ||
    paginationAudit.invalidPages.length ||
    paginationAudit.summaryCount > 1 ||
    !paginationAudit.summaryIsFinalBlock
  ) {
    throw new Error(`Invalid note pagination: ${JSON.stringify(paginationAudit)}`);
  }

  await page.pdf({
    path: outputPath,
    format: 'A4',
    printBackground: true,
    preferCSSPageSize: true,
    margin: {
      top: '0mm',
      right: '0mm',
      bottom: '0mm',
      left: '0mm',
    },
  });
} finally {
  await browser.close();
}
