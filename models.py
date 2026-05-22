from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

db = SQLAlchemy()


class Empresa(db.Model):
    __tablename__ = 'empresas'
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False)
    cnpj = db.Column(db.String(20), unique=True, nullable=False)
    ativo = db.Column(db.Boolean, default=True)
    usuarios = db.relationship('Usuario', backref='empresa', lazy=True)
    produtos = db.relationship('Produto', backref='empresa', lazy=True)
    ciclos = db.relationship('CicloContagem', backref='empresa', lazy=True)


class Usuario(UserMixin, db.Model):
    __tablename__ = 'usuarios'
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresas.id'), nullable=False)
    nome = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    senha_hash = db.Column(db.String(256), nullable=False)
    perfil = db.Column(db.String(20), nullable=False, default='estoquista')
    ativo = db.Column(db.Boolean, default=True)
    deve_trocar_senha = db.Column(db.Boolean, default=True)

    def set_senha(self, senha):
        self.senha_hash = generate_password_hash(senha)

    def check_senha(self, senha):
        return check_password_hash(self.senha_hash, senha)


class Produto(db.Model):
    __tablename__ = 'produtos'
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresas.id'), nullable=False)
    codigo = db.Column(db.String(50), nullable=False)
    descricao = db.Column(db.String(200), nullable=False)
    unidade = db.Column(db.String(10), default='UN')
    corredor = db.Column(db.String(20))
    prateleira = db.Column(db.String(20))
    ativo = db.Column(db.Boolean, default=True)

    __table_args__ = (db.UniqueConstraint('empresa_id', 'codigo'),)


class CicloContagem(db.Model):
    __tablename__ = 'ciclos_contagem'
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresas.id'), nullable=False)
    data_referencia = db.Column(db.Date, nullable=False)
    data_criacao = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20), default='aberto')  # aberto / em_contagem / em_revisao / fechado
    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'))
    itens = db.relationship('ItemCiclo', backref='ciclo', lazy=True, cascade='all, delete-orphan')


class ItemCiclo(db.Model):
    __tablename__ = 'itens_ciclo'
    id = db.Column(db.Integer, primary_key=True)
    ciclo_id = db.Column(db.Integer, db.ForeignKey('ciclos_contagem.id'), nullable=False)
    produto_id = db.Column(db.Integer, db.ForeignKey('produtos.id'), nullable=False)
    quantidade_esperada = db.Column(db.Float, nullable=False, default=0)
    status = db.Column(db.String(20), default='pendente')  # pendente / contado / divergente / segunda_contagem / aprovado / ajustado
    produto = db.relationship('Produto', lazy=True)
    registros = db.relationship('RegistroContagem', backref='item', lazy=True, cascade='all, delete-orphan')
    ajuste = db.relationship('AjusteDivergencia', backref='item', uselist=False, cascade='all, delete-orphan')


class RegistroContagem(db.Model):
    __tablename__ = 'registros_contagem'
    id = db.Column(db.Integer, primary_key=True)
    item_ciclo_id = db.Column(db.Integer, db.ForeignKey('itens_ciclo.id'), nullable=False)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False)
    quantidade_contada = db.Column(db.Float, nullable=False)
    rodada = db.Column(db.Integer, nullable=False, default=1)  # 1 = primeira, 2 = segunda
    data_contagem = db.Column(db.DateTime, default=datetime.utcnow)
    usuario = db.relationship('Usuario', lazy=True)


class AjusteDivergencia(db.Model):
    __tablename__ = 'ajustes_divergencia'
    id = db.Column(db.Integer, primary_key=True)
    item_ciclo_id = db.Column(db.Integer, db.ForeignKey('itens_ciclo.id'), nullable=False)
    quantidade_ajustada = db.Column(db.Float, nullable=False)
    justificativa = db.Column(db.Text, nullable=False)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False)
    data_ajuste = db.Column(db.DateTime, default=datetime.utcnow)
    usuario = db.relationship('Usuario', lazy=True)
