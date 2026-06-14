-- 0002_seed_quiz.sql -- bot-defence quiz content seed (formerly quiz_bank.json).
-- INSERT OR IGNORE: idempotent, and never clobbers questions edited via admin CRUD.
-- Per-site topics: ludo (Odoo migration), simplify-erp (Odoo consulting), re-cloud
-- (cloud); odoo is a generic English fallback. Future content ships as new patches.

-- topic: ludo
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('ludo', 'was-ist-odoo', 'Was ist Odoo?', '["Eine Business-/ERP-Software", "Ein Videospiel", "Ein Webbrowser", "Eine Kaffeemaschine"]', 0);
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('ludo', 'ludo-leistung', 'Wobei unterstützt Sie LUDO?', '["Bei der Migration auf Odoo", "Beim Pizzabacken", "Bei der Steuererklärung des Nachbarn", "Beim Möbelaufbau"]', 0);
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('ludo', 'migration-bedeutung', 'Was bedeutet ''Migration'' im IT-Kontext?', '["Umzug von Daten/System auf eine neue Plattform", "Der Vogelzug im Herbst", "Ein Tanzstil", "Eine Diätform"]', 0);
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('ludo', 'odoo-sprache', 'In welcher Programmiersprache ist Odoo hauptsächlich geschrieben?', '["Python", "COBOL", "Swift", "Latein"]', 0);
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('ludo', 'odoo-editionen', 'Welche zwei Editionen gibt es bei Odoo?', '["Community & Enterprise", "Bronze & Gold", "Lite & Pro", "Tag & Nacht"]', 0);
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('ludo', 'erp-bedeutung', 'Wofür steht die Abkürzung ''ERP''?', '["Enterprise Resource Planning", "Extra Rapid Pizza", "Elektronisches Routing-Protokoll", "Einfaches Reparatur-Programm"]', 0);
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('ludo', 'migration-ziel', 'Worauf migriert LUDO Ihr bestehendes System typischerweise?', '["Auf Odoo bzw. eine neuere Odoo-Version", "Auf einen Faxserver", "Auf eine Diskette", "Auf einen anderen Planeten"]', 0);

-- topic: simplify-erp
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('simplify-erp', 'angebot', 'Was bietet simplify-erp an?', '["Professionelle Odoo-Dienstleistungen & Entwicklung", "Pizzalieferung", "Autoreparaturen", "Reisebuchungen"]', 0);
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('simplify-erp', 'odoo-zweck', 'Odoo ist eine Software für…', '["Unternehmensverwaltung (ERP/CRM)", "Videobearbeitung", "Wetterforschung", "Musikstreaming"]', 0);
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('simplify-erp', 'echte-app', 'Welche dieser Odoo-Apps gibt es wirklich?', '["CRM", "Photoshop", "Spotify", "Minesweeper"]', 0);
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('simplify-erp', 'community-lizenz', 'Die Odoo Community Edition ist…', '["Open Source", "Nur Hardware", "Ein Mobile Game", "Closed Source"]', 0);
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('simplify-erp', 'erp-bedeutung', 'Wofür steht das ''ERP'' in Odoos Kategorie?', '["Enterprise Resource Planning", "Easy Repair Program", "Electronic Routing Protocol", "Extra Rapid Pizza"]', 0);
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('simplify-erp', 'odoo-sprache', 'In welcher Sprache wird Odoo hauptsächlich entwickelt?', '["Python", "Rust", "COBOL", "Swift"]', 0);
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('simplify-erp', 'consultant', 'Was macht ein Odoo-Consultant?', '["Berät und passt Odoo für Unternehmen an", "Backt Brot", "Repariert Fahrräder", "Komponiert Musik"]', 0);

-- topic: re-cloud
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('re-cloud', 'was-ist-recloud', 'Was beschreibt ''re-cloud'' am besten?', '["Ein Cloud-Betriebssystem / eine Cloud-Plattform", "Ein Wetterdienst für Wolken", "Ein Gartenschlauch", "Ein Brettspiel"]', 0);
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('re-cloud', 'cloud-bedeutung', 'Was bedeutet ''Cloud'' in der IT?', '["Bereitstellung von IT-Ressourcen über das Internet", "Eine echte Regenwolke", "Ein anderes Wort für Festplatte", "Ein Smartphone-Modell"]', 0);
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('re-cloud', 'cloud-vorteil', 'Was ist ein typischer Vorteil von Cloud-Diensten?', '["Skalierbarkeit und Zugriff von überall", "Man braucht kein Internet", "Daten verschwinden automatisch", "Es wird kein Strom benötigt"]', 0);
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('re-cloud', 'saas', 'Wofür steht die Abkürzung ''SaaS''?', '["Software as a Service", "Sausage and Salad", "Secure Automatic Server", "System and Storage"]', 0);
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('re-cloud', 'datenspeicher', 'Wo werden Daten in der Cloud gespeichert?', '["In entfernten Rechenzentren", "Im Inneren der Tastatur", "Auf dem Mond", "In der Mikrowelle"]', 0);
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('re-cloud', 'betriebssystem', 'Was ist ein Betriebssystem?', '["Software, die Hardware und Programme verwaltet", "Eine Tabellenkalkulation", "Ein Druckerkabel", "Ein USB-Stick"]', 0);
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('re-cloud', 'abrechnung', 'Cloud-Ressourcen werden meist abgerechnet…', '["Nutzungsbasiert (pay as you go)", "Pro Mondphase", "Nach Körpergröße", "Gar nicht"]', 0);

-- topic: odoo
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('odoo', 'what-is-odoo', 'What kind of software is Odoo?', '["A business / ERP suite", "A video game", "A web browser", "A coffee machine"]', 0);
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('odoo', 'editions', 'Which two editions does Odoo come in?', '["Community & Enterprise", "Bronze & Gold", "Lite & Pro", "Home & Away"]', 0);
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('odoo', 'language', 'Odoo''s backend is mainly written in which language?', '["Python", "COBOL", "Swift", "Rust"]', 0);
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('odoo', 'is-app', 'Which of these is a real Odoo app?', '["CRM", "Photoshop", "Spotify", "Minesweeper"]', 0);
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('odoo', 'erp-meaning', 'What does the ''ERP'' in Odoo''s category stand for?', '["Enterprise Resource Planning", "Electronic Routing Protocol", "Extra Rapid Pizza", "Easy Repair Program"]', 0);
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('odoo', 'community-license', 'Odoo Community edition is…', '["Open source", "Closed source", "Hardware only", "A mobile game"]', 0);
INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ('odoo', 'migration', 'When you ''migrate'' Odoo, you move it to a…', '["Newer version", "Different planet", "Spreadsheet", "Fax machine"]', 0);
