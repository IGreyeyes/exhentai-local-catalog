import {DatabaseSync} from "node:sqlite";
import {readFileSync} from "node:fs";

// 真实的临时内存 SQLite 执行生产 SQL，适配 D1 接口；不访问 Cloudflare 或真实资料库。
export class D1 {
  constructor() {
    this.sqlite = new DatabaseSync(":memory:");
    this.sqlite.exec(readFileSync(new URL("../cloudflare/schema.sql", import.meta.url), "utf8"));
  }
  prepare(sql) {
    const db = this;
    return new class {
      values = [];
      bind(...values) { this.values = values; return this; }
      async first() { return db.sqlite.prepare(sql).get(...this.values) ?? null; }
      async all() { return {results: db.sqlite.prepare(sql).all(...this.values)}; }
      async run() {
        let stmt;try{stmt=db.sqlite.prepare(sql);}catch(e){throw new Error(e.message+" SQL "+sql);}
        // D1 metadata may include trigger writes; RETURNING rows describe only the target statement.
        const before = db.sqlite.prepare('SELECT total_changes() AS n').get().n;
        const results = stmt.columns().length ? stmt.all(...this.values) : (stmt.run(...this.values), []);
        const after = db.sqlite.prepare('SELECT total_changes() AS n').get().n;
        return {success: true, results, meta: {changes: Number(after-before)}};
      }
    }();
  }
  async batch(statements) {
    this.sqlite.exec("BEGIN IMMEDIATE");
    try {
      const results = [];
      for (const statement of statements) results.push(await statement.run());
      if (this.failBatch) throw Error("test-only simulated failure");
      this.sqlite.exec("COMMIT");
      return results;
    } catch (error) { this.lastError=error.message; this.sqlite.exec("ROLLBACK"); throw error; }
  }
  close() { this.sqlite.close(); }
}
