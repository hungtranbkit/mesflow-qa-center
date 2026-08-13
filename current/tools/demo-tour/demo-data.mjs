export function makeDemoData() {
  const stamp = new Date().toISOString().replace(/\D/g, '').slice(8, 14);
  return {
    prefix: 'DEMO-VIDEO-',
    employeeCode: `DEMO-VIDEO-NV01-${stamp}`,
    employeeName: 'Nguyễn Văn An',
    templateCode: `DEMO-VIDEO-HOP-THEP-${stamp}`,
    templateName: 'Hộp thép hướng dẫn',
    poCode: `DEMO-VIDEO-PO-${stamp}`,
    partCode: `DEMO-VIDEO-KHUNG-${stamp}`,
    partName: 'KHUNG MÁY',
    plannedQuantity: 100,
    operations: [
      ['DV-CAT', 'Cắt laser'], ['DV-DAP', 'Dập'], ['DV-CHAN', 'Chấn'],
      ['DV-HAN', 'Hàn'], ['DV-DAN-KEO', 'Dán keo'], ['DV-DONG-GOI', 'Đóng gói'],
    ].map(([suffix, name], index) => ({ code: `${suffix}-${stamp}`, name, seconds: 30 + index * 5 })),
  };
}
