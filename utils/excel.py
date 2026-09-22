"""Export untrusted text as text, never as spreadsheet formulas."""
def save_workbook(workbook, path):
    for sheet in workbook:
        for row in sheet:
            for cell in row:
                if cell.data_type == 'f':
                    cell.data_type = 's'
    workbook.save(path)
