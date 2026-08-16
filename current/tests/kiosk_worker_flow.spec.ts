import { test, expect, Page } from '@playwright/test';

async function openQuantity(page: Page, onFinish: (body: any) => Promise<number> | number) {
  await page.route('**/api/kiosk-web/heartbeat', route => route.fulfill({ json: { ok: true } }));
  await page.route('**/api/kiosk-web/scan', route => route.fulfill({ json: {
    ok: true, type: 'employee', employee: { id: 7, employee_no: 'QA-EMP-007', name: 'QA Worker' },
    open_session: { id: 901, operation_code: 'QA-OP-10', operation_name: 'Dập đế' }
  }}));
  await page.route('**/api/kiosk-web/finish/901', async route => {
    const status = await onFinish(route.request().postDataJSON());
    await route.fulfill({ status, json: status < 400 ? { ok: true } : { ok: false, message: 'temporary failure' } });
  });
  await page.goto('/kiosk');
  await page.locator('#scanner-input').fill('WF|EMP|QA-EMP-007');
  await page.locator('#scanner-input').press('Enter');
  await expect(page.locator('#screen-quantity-good')).toHaveClass(/active/);
}

async function enterGoodAndDefect(page: Page, good: number, defect: number) {
  await page.getByTestId('kiosk-good-quantity').fill(String(good));
  await page.locator('#good-next').click();
  await expect(page.locator('#screen-quantity-defect')).toHaveClass(/active/);
  await page.getByTestId('kiosk-defect-quantity').fill(String(defect));
  await page.locator('#defect-next').click();
}

test('Good 25, Defect 0 skips rework and submits zero rework', async ({ page }) => {
  let payload: any;
  await openQuantity(page, body => { payload = body; return 200; });
  await enterGoodAndDefect(page, 25, 0);
  await expect(page.locator('#screen-finish-confirm')).toHaveClass(/active/);
  await expect(page.getByTestId('kiosk-rework-choice-none')).not.toBeVisible();
  await page.getByTestId('kiosk-quantity-confirm').click();
  expect(payload).toMatchObject({ good_qty: 25, defect_qty: 0, rework_qty: 0 });
});

test('Defect 5 and Không, xong submits rework zero', async ({ page }) => {
  let payload: any;
  await openQuantity(page, body => { payload = body; return 200; });
  await enterGoodAndDefect(page, 25, 5);
  await expect(page.locator('#screen-ask-rework')).toHaveClass(/active/);
  await page.getByTestId('kiosk-rework-choice-none').click();
  await expect(page.locator('#finish-confirm-summary')).toContainText('Lỗi');
  await page.getByTestId('kiosk-quantity-confirm').click();
  expect(payload).toMatchObject({ good_qty: 25, defect_qty: 5, rework_qty: 0 });
});

test('Defect 5 and repairable 3 shows scrap 2 and submits backend semantics', async ({ page }) => {
  let payload: any;
  await openQuantity(page, body => { payload = body; return 200; });
  await enterGoodAndDefect(page, 25, 5);
  await page.getByTestId('kiosk-rework-choice-yes').click();
  await page.getByTestId('kiosk-rework-quantity').fill('3');
  await page.locator('#rework-next').click();
  const summary = page.locator('#finish-confirm-summary');
  await expect(summary).toContainText('Lỗi tổng');
  await expect(summary).toContainText('Sửa được');
  await expect(summary).toContainText('Phế');
  await expect(summary).toContainText('2');
  await page.getByTestId('kiosk-quantity-confirm').click();
  expect(payload).toMatchObject({ good_qty: 25, defect_qty: 5, rework_qty: 3 });
});

test('Rework greater than defect is blocked without submit', async ({ page }) => {
  let submitCount = 0;
  await openQuantity(page, () => { submitCount += 1; return 200; });
  await enterGoodAndDefect(page, 25, 5);
  await page.getByTestId('kiosk-rework-choice-yes').click();
  await page.getByTestId('kiosk-rework-quantity').fill('6');
  await page.locator('#rework-next').click();
  await expect(page.locator('#rework-validation')).toHaveText('Số lỗi sửa được không thể lớn hơn số sản phẩm lỗi');
  await expect(page.locator('#screen-quantity-rework')).toHaveClass(/active/);
  expect(submitCount).toBe(0);
});

test('Back from confirmation keeps quantities and returns to the logical step', async ({ page }) => {
  await openQuantity(page, () => 200);
  await enterGoodAndDefect(page, 25, 5);
  await page.getByTestId('kiosk-rework-choice-yes').click();
  await page.getByTestId('kiosk-rework-quantity').fill('3');
  await page.locator('#rework-next').click();
  await page.getByTestId('kiosk-quantity-back').click();
  await expect(page.locator('#screen-quantity-rework')).toHaveClass(/active/);
  await expect(page.getByTestId('kiosk-rework-quantity')).toHaveValue('3');
  await expect(page.getByTestId('kiosk-good-quantity')).toHaveValue('25');
  await expect(page.getByTestId('kiosk-defect-quantity')).toHaveValue('5');
});

test('Transient submit failure keeps quantities and retries exact idempotency payload', async ({ page }) => {
  const requests: any[] = [];
  await openQuantity(page, body => { requests.push(body); return requests.length === 1 ? 503 : 200; });
  await enterGoodAndDefect(page, 25, 5);
  await page.getByTestId('kiosk-rework-choice-none').click();
  await page.getByTestId('kiosk-quantity-confirm').click();
  await expect(page.getByTestId('kiosk-submit-retry')).toBeVisible();
  await expect(page.locator('#finish-submit-error')).toHaveText('CHƯA GỬI ĐƯỢC SẢN LƯỢNG');
  await page.getByTestId('kiosk-submit-retry').click();
  await expect(page.locator('#screen-finished')).toHaveClass(/active/);
  expect(requests).toHaveLength(2);
  expect(requests[1]).toEqual(requests[0]);
});

test('Zero output remains valid', async ({ page }) => {
  let payload: any;
  await openQuantity(page, body => { payload = body; return 200; });
  await enterGoodAndDefect(page, 0, 0);
  await page.getByTestId('kiosk-quantity-confirm').click();
  expect(payload).toMatchObject({ good_qty: 0, defect_qty: 0, rework_qty: 0 });
});

for (const viewport of [
  { name: 'FHD', width: 1920, height: 1080 },
  { name: 'laptop', width: 1366, height: 768 },
  { name: 'mobile', width: 390, height: 844 },
]) {
  test(`Kiosk quantity flow has no horizontal overflow at ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await openQuantity(page, () => 200);
    await enterGoodAndDefect(page, 25, 5);
    await page.getByTestId('kiosk-rework-choice-none').click();
    const dimensions = await page.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
    expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.width);
    await expect(page.getByTestId('kiosk-quantity-confirm')).toBeInViewport();
  });
}

test('Double click Finish sends one in-flight request', async ({ page }) => {
  let requestCount = 0;
  await openQuantity(page, async () => {
    requestCount += 1;
    await new Promise(resolve => setTimeout(resolve, 250));
    return 200;
  });
  await enterGoodAndDefect(page, 3, 1);
  await page.getByTestId('kiosk-rework-choice-none').click();
  await page.getByTestId('kiosk-quantity-confirm').dblclick({ delay: 25 });
  await expect(page.locator('#screen-finished')).toHaveClass(/active/);
  expect(requestCount).toBe(1);
});
