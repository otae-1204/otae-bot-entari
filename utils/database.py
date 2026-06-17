import sqlite3
from configs.path_config import SQL_PATH
import os

class SQLiteHelper:
    def __init__(self, db_name: str, sql_path:str=SQL_PATH):
        """初始化数据库连接"""
        # 数据库文件路径
        if not os.path.exists(sql_path):
            os.makedirs(sql_path)
        self.db_name = os.path.join(sql_path, db_name)
        self.connection: None | sqlite3.Connection = None
        self.cursor: None | sqlite3.Cursor = None

    def connect(self):
        """建立数据库连接"""
        if not self.connection:
            self.connection = sqlite3.connect(self.db_name)
            self.cursor = self.connection.cursor()

    def close(self):
        """关闭数据库连接"""
        if self.connection:
            self.connection.commit()
            self.connection.close()
            self.connection = None
            self.cursor: None | sqlite3.Cursor = None

    def execute(self, query, params=None):
        """执行 SQL 查询（非查询型语句如 INSERT、UPDATE）"""
        self.connect()
        if params is None:
            self.cursor.execute(query)
        else:
            self.cursor.execute(query, params)
        self.connection.commit()

    def fetchall(self, query, params=None):
        """执行 SELECT 查询并返回所有结果"""
        self.connect()
        if params is None:
            self.cursor.execute(query)
        else:
            self.cursor.execute(query, params)
        return self.cursor.fetchall()

    def fetchone(self, query, params=None):
        """执行 SELECT 查询并返回一条结果"""
        self.connect()
        if params is None:
            self.cursor.execute(query)
        else:
            self.cursor.execute(query, params)
        return self.cursor.fetchone()

    def executemany(self, query, param_list):
        """批量执行 SQL 语句"""
        self.connect()
        self.cursor.executemany(query, param_list)
        self.connection.commit()

    def create_table(self, table_name, columns):
        """创建表"""
        columns_def = ', '.join([f"{col} {col_type}" for col, col_type in columns.items()])
        query = f"CREATE TABLE IF NOT EXISTS {table_name} ({columns_def})"
        self.execute(query)

    def insert(self, table_name, data):
        """插入数据"""
        placeholders = ', '.join(['?'] * len(data))
        columns = ', '.join(data.keys())
        values = tuple(data.values())
        query = f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders})"
        self.execute(query, values)

# 示例用法
if __name__ == "__main__":
    db = SQLiteHelper('example.db')

    # 创建表
    db.create_table('users', {
        'id': 'INTEGER PRIMARY KEY',
        'name': 'TEXT',
        'age': 'INTEGER'
    })

    # 插入数据
    db.insert('users', {'name': 'Alice', 'age': 25})
    db.insert('users', {'name': 'Bob', 'age': 30})

    # 查询数据
    users = db.fetchall("SELECT * FROM users")
    print(users)

    # 更新和删除示例
    db.execute("UPDATE users SET age = ? WHERE name = ?", (26, 'Alice'))
    user = db.fetchone("SELECT * FROM users WHERE name = ?", ('Alice',))
    print(user)

    # 关闭数据库连接
    db.close()
