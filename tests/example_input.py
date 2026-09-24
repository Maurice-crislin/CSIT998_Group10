def get_f5d_science_scores(db):
    students = db.execute("SELECT student_number, student_name FROM students WHERE form = 5 AND class = 'D'")
    report = []
    for s in students:
        scores = db.execute(f"SELECT score FROM student_scores WHERE student_number = {s.student_number} AND subject = 'Science'")
        for sc in scores:
            report.append({'student_number': s.student_number, 'name': s.student_name, 'score': sc.score})
    return report
