"""Серверные тесты бизнес-правил. Запуск: python -m unittest -v"""
import os, tempfile, threading, unittest
import app
from app import ApiError


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        app.DB = os.path.join(self.dir.name, "t.db")
        app.init_db()
        self.c = app.connect()
        reg = lambda n: app.register(self.c, {"email": f"{n}@t.ru", "name": n, "password": "secret1"})
        self.author, self.u1, self.u2, self.out = (reg(n) for n in ("author", "u1", "u2", "out"))
        self.pid = app.create_project(self.c, self.author, {
            "title": "P", "description": "D", "goal": "G", "category": "Технологии", "publish": True,
            "vacancies": [{"role": "Dev", "seats": 1}, {"role": "Designer", "seats": 1}]})
        self.v1, self.v2 = (v["id"] for v in app.vacs(self.c, self.pid))

    def tearDown(self): self.c.close(); self.dir.cleanup()

    def code(self, fn, *a):
        with self.assertRaises(ApiError) as e: fn(self.c, *a)
        return e.exception.code

    def apply(self, uid, vid=None, msg="Привет", c=None): return app.apply(self.c, uid, self.pid, {"vacancy_id": vid or self.v1, "message": msg})
    def apply_c(self, c, uid, vid=None, msg="Привет"): return self.apply(uid, vid, msg)
    def n(self, table, where="1"): return self.c.execute(f"select count(*) n from {table} where {where}").fetchone()["n"]


class Rights(Base):
    def test_only_author_processes(self):
        """Ловит: заявку может принять посторонний или сам кандидат."""
        a = self.apply(self.u1)
        self.assertEqual(self.code(app.decide, self.out, a, "accept"), 403)
        self.assertEqual(self.code(app.decide, self.u1, a, "accept"), 403)
        self.assertEqual(self.code(app.decide, self.out, a, "reject"), 403)
        self.assertEqual(self.n("members"), 0)
        app.decide(self.c, self.author, a, "accept")
        self.assertEqual(self.n("members"), 1)

    def test_foreign_edit_and_applications_forbidden(self):
        """Ловит: чужой проект можно править, чужие заявки читать, автор подаёт заявку себе."""
        self.assertEqual(self.code(app.update_project, self.out, self.pid, {"title": "x", "description": "x", "goal": "x", "category": "Другое"}), 403)
        self.assertEqual(self.code(app.project_apps, self.out, self.pid, ""), 403)
        self.assertEqual(self.code(app.set_open, self.out, self.pid, False), 403)
        self.assertEqual(self.code(app.apply, self.author, self.pid, {"vacancy_id": self.v1, "message": "я"}), 403)
        a = self.apply(self.u1)
        self.assertEqual(self.code(app.decide, self.u2, a, "withdraw"), 403)


class OnePending(Base):
    def test_single_active_application(self):
        """Ловит: дубль запроса или заявка на другую вакансию создают вторую активную заявку."""
        self.apply(self.u1)
        self.assertEqual(self.code(self.apply_c, self.u1), 409)
        self.assertEqual(self.code(self.apply_c, self.u1, self.v2), 409)
        self.assertEqual(self.n("applications", "status='pending'"), 1)

    def test_reapply_after_reject_keeps_history(self):
        """Ловит: потерю истории или запрет повторной подачи после отклонения."""
        a = self.apply(self.u1); app.decide(self.c, self.author, a, "reject")
        self.apply(self.u1, self.v2)
        self.assertEqual(self.n("applications"), 2)


class Capacity(Base):
    def test_last_seat_sequential(self):
        """Ловит: принятие второй заявки на последнее место."""
        a, b = self.apply(self.u1), self.apply(self.u2)
        app.decide(self.c, self.author, a, "accept")
        self.assertEqual(self.code(app.decide, self.author, b, "accept"), 409)
        self.assertEqual(self.n("members"), 1)

    def test_last_seat_concurrent(self):
        """Ловит: гонку — два почти одновременных принятия на одно место."""
        a, b = self.apply(self.u1), self.apply(self.u2)
        res, bar = [], threading.Barrier(2)
        def go(aid):
            c = app.connect(); bar.wait()
            try: app.decide(c, self.author, aid, "accept"); res.append("ok")
            except ApiError as e: res.append(e.code)
            finally: c.close()
        ts = [threading.Thread(target=go, args=(x,)) for x in (a, b)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(sorted(map(str, res)), ["409", "ok"])
        self.assertEqual(self.n("members"), 1)

    def test_repeat_accept_no_duplicate(self):
        """Ловит: повторное принятие дублирует участника или занимает второе место."""
        a = self.apply(self.u1); app.decide(self.c, self.author, a, "accept")
        self.assertEqual(self.code(app.decide, self.author, a, "accept"), 409)
        self.assertEqual(self.n("members"), 1)
        self.assertEqual(app.vacs(self.c, self.pid)[0]["free"], 0)

    def test_seats_not_below_accepted(self):
        """Ловит: уменьшение мест ниже числа принятых и удаление вакансии с данными."""
        a = self.apply(self.u1); app.decide(self.c, self.author, a, "accept")
        app.update_vacancy(self.c, self.author, self.v1, {"role": "Dev", "seats": 2})
        self.assertEqual(self.code(app.delete_vacancy, self.author, self.v1), 409)


class Closed(Base):
    def test_closed_blocks_apply_and_accept(self):
        """Ловит: заявки отправляются или принимаются при закрытом наборе; закрытие теряет данные."""
        a = self.apply(self.u1)
        app.set_open(self.c, self.author, self.pid, False)
        self.assertEqual(self.code(self.apply_c, self.u2), 409)
        self.assertEqual(self.code(app.decide, self.author, a, "accept"), 409)
        self.assertEqual(self.n("applications", "status='pending'"), 1)
        app.set_open(self.c, self.author, self.pid, True)
        app.decide(self.c, self.author, a, "accept")
        self.assertEqual(self.n("members"), 1)


class Validation(Base):
    def test_bad_input_rejected(self):
        """Ловит: пустые строки, сообщение из пробелов, 0/отрицательное/дробное число мест."""
        P = {"title": "T", "description": "D", "goal": "G", "category": "Другое"}
        for bad_p in ({**P, "title": " "}, {**P, "description": ""}):
            self.assertEqual(self.code(app.create_project, self.author, bad_p), 400)
        for s in (0, -1, 1.5, "2", None):
            self.assertEqual(self.code(app.add_vacancy, self.author, self.pid, {"role": "R", "seats": s}), 400)
        self.assertEqual(self.code(self.apply_c, self.u1, None, "   "), 400)
        self.assertEqual(self.code(app.create_project, self.author, {**P, "publish": True}), 400)  # нет вакансий


if __name__ == "__main__":
    unittest.main()
