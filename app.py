import os
import io
from dotenv import load_dotenv
load_dotenv()
from datetime import date, datetime
from flask import Flask, render_template, redirect, url_for, request, flash, send_file, jsonify
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from functools import wraps
import pandas as pd
from models import db, Empresa, Usuario, Produto, CicloContagem, ItemCiclo, RegistroContagem, AjusteDivergencia

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'chave-secreta-estoque-2024')

_db_url = os.environ.get('DATABASE_URL', 'sqlite:///estoque.db')
if _db_url.startswith('postgres://'):
    _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'uploads'

db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Faça login para acessar.'
login_manager.login_message_category = 'warning'


@login_manager.user_loader
def load_user(user_id):
    return Usuario.query.get(int(user_id))


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.perfil != 'admin':
            flash('Acesso restrito a administradores.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


# ── AUTH ──────────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        senha = request.form.get('senha', '')
        usuario = Usuario.query.filter_by(email=email, ativo=True).first()
        if usuario and usuario.check_senha(senha):
            login_user(usuario, remember=True)
            return redirect(url_for('dashboard'))
        flash('Email ou senha incorretos.', 'danger')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


# ── DASHBOARD ─────────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def dashboard():
    ciclos = CicloContagem.query.filter_by(empresa_id=current_user.empresa_id)\
        .order_by(CicloContagem.data_referencia.desc()).limit(10).all()
    ciclo_ativo = CicloContagem.query.filter(
        CicloContagem.empresa_id == current_user.empresa_id,
        CicloContagem.status.in_(['aberto', 'em_contagem', 'em_revisao'])
    ).order_by(CicloContagem.data_referencia.desc()).first()
    return render_template('dashboard.html', ciclos=ciclos, ciclo_ativo=ciclo_ativo)


# ── IMPORTAR MOVIMENTAÇÃO ─────────────────────────────────────────────────────

@app.route('/importar', methods=['GET', 'POST'])
@login_required
@admin_required
def importar():
    if request.method == 'POST':
        arquivo = request.files.get('arquivo')
        data_ref_str = request.form.get('data_referencia')
        if not arquivo or not data_ref_str:
            flash('Selecione o arquivo e a data de referência.', 'warning')
            return redirect(url_for('importar'))

        try:
            data_ref = datetime.strptime(data_ref_str, '%Y-%m-%d').date()
        except ValueError:
            flash('Data inválida.', 'danger')
            return redirect(url_for('importar'))

        existente = CicloContagem.query.filter_by(
            empresa_id=current_user.empresa_id,
            data_referencia=data_ref
        ).first()
        if existente:
            flash(f'Já existe um ciclo para {data_ref.strftime("%d/%m/%Y")}.', 'warning')
            return redirect(url_for('importar'))

        try:
            df = pd.read_excel(arquivo, dtype={'Codigo': str})
            colunas_esperadas = {'Codigo', 'Descricao', 'Quantidade'}
            if not colunas_esperadas.issubset(set(df.columns)):
                flash(f'O arquivo deve ter as colunas: Codigo, Descricao, Quantidade', 'danger')
                return redirect(url_for('importar'))

            ciclo = CicloContagem(
                empresa_id=current_user.empresa_id,
                data_referencia=data_ref,
                status='aberto',
                criado_por_id=current_user.id
            )
            db.session.add(ciclo)
            db.session.flush()

            total = 0
            for _, row in df.iterrows():
                codigo = str(row['Codigo']).strip()
                descricao = str(row['Descricao']).strip()
                try:
                    quantidade = float(row['Quantidade'])
                except (ValueError, TypeError):
                    quantidade = 0.0

                corredor = str(row.get('Corredor', '')).strip() if 'Corredor' in df.columns else ''
                prateleira = str(row.get('Prateleira', '')).strip() if 'Prateleira' in df.columns else ''
                unidade = str(row.get('Unidade', 'UN')).strip() if 'Unidade' in df.columns else 'UN'

                produto = Produto.query.filter_by(
                    empresa_id=current_user.empresa_id,
                    codigo=codigo
                ).first()
                if not produto:
                    produto = Produto(
                        empresa_id=current_user.empresa_id,
                        codigo=codigo,
                        descricao=descricao,
                        unidade=unidade,
                        corredor=corredor or None,
                        prateleira=prateleira or None
                    )
                    db.session.add(produto)
                    db.session.flush()
                else:
                    produto.descricao = descricao
                    if corredor:
                        produto.corredor = corredor
                    if prateleira:
                        produto.prateleira = prateleira

                item = ItemCiclo(
                    ciclo_id=ciclo.id,
                    produto_id=produto.id,
                    quantidade_esperada=quantidade,
                    status='pendente'
                )
                db.session.add(item)
                total += 1

            db.session.commit()
            flash(f'Ciclo criado com {total} produtos. Data: {data_ref.strftime("%d/%m/%Y")}', 'success')
            return redirect(url_for('dashboard'))

        except Exception as e:
            db.session.rollback()
            flash(f'Erro ao processar arquivo: {str(e)}', 'danger')

    return render_template('importar.html', today=date.today().isoformat())


@app.route('/modelo-excel')
@login_required
@admin_required
def baixar_modelo():
    rows = [
        {'Codigo': '001', 'Descricao': 'Produto Exemplo A', 'Quantidade': 100,
         'Corredor': 'A', 'Prateleira': '01', 'Unidade': 'UN'},
        {'Codigo': '002', 'Descricao': 'Produto Exemplo B', 'Quantidade': 250,
         'Corredor': 'A', 'Prateleira': '02', 'Unidade': 'CX'},
    ]
    df = pd.DataFrame(rows)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Movimentacao')
    output.seek(0)
    return send_file(output, as_attachment=True, download_name='modelo_movimentacao.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ── CONTAGEM ──────────────────────────────────────────────────────────────────

@app.route('/contagem/<int:ciclo_id>')
@login_required
def contagem(ciclo_id):
    ciclo = CicloContagem.query.filter_by(
        id=ciclo_id, empresa_id=current_user.empresa_id
    ).first_or_404()
    filtro_corredor = request.args.get('corredor', '')
    query = ItemCiclo.query.filter(
        ItemCiclo.ciclo_id == ciclo_id,
        ItemCiclo.status.in_(['pendente', 'contado', 'divergente'])
    )
    if filtro_corredor:
        query = query.join(Produto).filter(Produto.corredor == filtro_corredor)
    itens = query.all()
    corredores = db.session.query(Produto.corredor).join(ItemCiclo).filter(
        ItemCiclo.ciclo_id == ciclo_id,
        Produto.corredor.isnot(None)
    ).distinct().all()
    corredores = [c[0] for c in corredores if c[0]]
    return render_template('contagem.html', ciclo=ciclo, itens=itens,
                           corredores=corredores, filtro_corredor=filtro_corredor)


@app.route('/contagem/registrar', methods=['POST'])
@login_required
def registrar_contagem():
    item_id = request.form.get('item_id', type=int)
    quantidade = request.form.get('quantidade', type=float)
    ciclo_id = request.form.get('ciclo_id', type=int)

    if item_id is None or quantidade is None:
        flash('Dados inválidos.', 'danger')
        return redirect(url_for('contagem', ciclo_id=ciclo_id))

    item = ItemCiclo.query.join(CicloContagem).filter(
        ItemCiclo.id == item_id,
        CicloContagem.empresa_id == current_user.empresa_id
    ).first_or_404()

    registro_existente = RegistroContagem.query.filter_by(
        item_ciclo_id=item_id, rodada=1
    ).first()
    if registro_existente:
        registro_existente.quantidade_contada = quantidade
        registro_existente.usuario_id = current_user.id
        registro_existente.data_contagem = datetime.utcnow()
    else:
        registro = RegistroContagem(
            item_ciclo_id=item_id,
            usuario_id=current_user.id,
            quantidade_contada=quantidade,
            rodada=1
        )
        db.session.add(registro)

    if quantidade != item.quantidade_esperada:
        item.status = 'divergente'
    else:
        item.status = 'contado'

    db.session.commit()
    return redirect(url_for('contagem', ciclo_id=ciclo_id))


@app.route('/ciclo/<int:ciclo_id>/gerar-segunda-contagem', methods=['POST'])
@login_required
@admin_required
def gerar_segunda_contagem(ciclo_id):
    ciclo = CicloContagem.query.filter_by(
        id=ciclo_id, empresa_id=current_user.empresa_id
    ).first_or_404()
    divergentes = ItemCiclo.query.filter_by(ciclo_id=ciclo_id, status='divergente').all()
    for item in divergentes:
        item.status = 'segunda_contagem'
    ciclo.status = 'em_revisao'
    db.session.commit()
    flash(f'{len(divergentes)} item(ns) enviado(s) para segunda contagem.', 'success')
    return redirect(url_for('segunda_contagem', ciclo_id=ciclo_id))


# ── SEGUNDA CONTAGEM ──────────────────────────────────────────────────────────

@app.route('/segunda-contagem/<int:ciclo_id>')
@login_required
def segunda_contagem(ciclo_id):
    ciclo = CicloContagem.query.filter_by(
        id=ciclo_id, empresa_id=current_user.empresa_id
    ).first_or_404()
    itens = ItemCiclo.query.filter_by(ciclo_id=ciclo_id, status='segunda_contagem').all()
    return render_template('segunda_contagem.html', ciclo=ciclo, itens=itens)


@app.route('/segunda-contagem/registrar', methods=['POST'])
@login_required
def registrar_segunda_contagem():
    item_id = request.form.get('item_id', type=int)
    quantidade = request.form.get('quantidade', type=float)
    ciclo_id = request.form.get('ciclo_id', type=int)

    if item_id is None or quantidade is None:
        flash('Dados inválidos.', 'danger')
        return redirect(url_for('segunda_contagem', ciclo_id=ciclo_id))

    item = ItemCiclo.query.join(CicloContagem).filter(
        ItemCiclo.id == item_id,
        CicloContagem.empresa_id == current_user.empresa_id
    ).first_or_404()

    registro_existente = RegistroContagem.query.filter_by(
        item_ciclo_id=item_id, rodada=2
    ).first()
    if registro_existente:
        registro_existente.quantidade_contada = quantidade
        registro_existente.usuario_id = current_user.id
        registro_existente.data_contagem = datetime.utcnow()
    else:
        registro = RegistroContagem(
            item_ciclo_id=item_id,
            usuario_id=current_user.id,
            quantidade_contada=quantidade,
            rodada=2
        )
        db.session.add(registro)

    if quantidade == item.quantidade_esperada:
        item.status = 'aprovado'
    else:
        item.status = 'divergente'

    db.session.commit()
    return redirect(url_for('segunda_contagem', ciclo_id=ciclo_id))


# ── DIVERGÊNCIAS ──────────────────────────────────────────────────────────────

@app.route('/divergencias/<int:ciclo_id>')
@login_required
def divergencias(ciclo_id):
    ciclo = CicloContagem.query.filter_by(
        id=ciclo_id, empresa_id=current_user.empresa_id
    ).first_or_404()
    itens = ItemCiclo.query.filter_by(ciclo_id=ciclo_id, status='divergente').all()
    return render_template('divergencias.html', ciclo=ciclo, itens=itens)


@app.route('/divergencias/ajustar', methods=['POST'])
@login_required
@admin_required
def ajustar_divergencia():
    item_id = request.form.get('item_id', type=int)
    quantidade_ajustada = request.form.get('quantidade_ajustada', type=float)
    justificativa = request.form.get('justificativa', '').strip()
    ciclo_id = request.form.get('ciclo_id', type=int)

    if not item_id or quantidade_ajustada is None or not justificativa:
        flash('Preencha todos os campos do ajuste.', 'warning')
        return redirect(url_for('divergencias', ciclo_id=ciclo_id))

    item = ItemCiclo.query.join(CicloContagem).filter(
        ItemCiclo.id == item_id,
        CicloContagem.empresa_id == current_user.empresa_id
    ).first_or_404()

    if item.ajuste:
        item.ajuste.quantidade_ajustada = quantidade_ajustada
        item.ajuste.justificativa = justificativa
        item.ajuste.usuario_id = current_user.id
        item.ajuste.data_ajuste = datetime.utcnow()
    else:
        ajuste = AjusteDivergencia(
            item_ciclo_id=item_id,
            quantidade_ajustada=quantidade_ajustada,
            justificativa=justificativa,
            usuario_id=current_user.id
        )
        db.session.add(ajuste)

    item.status = 'ajustado'
    db.session.commit()
    flash('Ajuste registrado com sucesso.', 'success')
    return redirect(url_for('divergencias', ciclo_id=ciclo_id))


@app.route('/ciclo/<int:ciclo_id>/fechar', methods=['POST'])
@login_required
@admin_required
def fechar_ciclo(ciclo_id):
    ciclo = CicloContagem.query.filter_by(
        id=ciclo_id, empresa_id=current_user.empresa_id
    ).first_or_404()
    pendentes = ItemCiclo.query.filter(
        ItemCiclo.ciclo_id == ciclo_id,
        ItemCiclo.status.in_(['pendente', 'divergente', 'segunda_contagem'])
    ).count()
    if pendentes > 0:
        flash(f'Ainda há {pendentes} item(ns) pendente(s). Resolva todas as divergências antes de fechar.', 'warning')
        return redirect(url_for('dashboard'))
    ciclo.status = 'fechado'
    db.session.commit()
    flash('Ciclo fechado com sucesso.', 'success')
    return redirect(url_for('dashboard'))


# ── RELATÓRIO / EXPORTAR ──────────────────────────────────────────────────────

@app.route('/relatorio/<int:ciclo_id>')
@login_required
def relatorio(ciclo_id):
    ciclo = CicloContagem.query.filter_by(
        id=ciclo_id, empresa_id=current_user.empresa_id
    ).first_or_404()
    itens = ItemCiclo.query.filter_by(ciclo_id=ciclo_id).all()
    return render_template('relatorio.html', ciclo=ciclo, itens=itens)


@app.route('/exportar/<int:ciclo_id>')
@login_required
def exportar(ciclo_id):
    ciclo = CicloContagem.query.filter_by(
        id=ciclo_id, empresa_id=current_user.empresa_id
    ).first_or_404()
    itens = ItemCiclo.query.filter_by(ciclo_id=ciclo_id).all()

    rows = []
    for item in itens:
        reg1 = next((r for r in item.registros if r.rodada == 1), None)
        reg2 = next((r for r in item.registros if r.rodada == 2), None)
        qtd_contada = reg2.quantidade_contada if reg2 else (reg1.quantidade_contada if reg1 else None)
        qtd_ajustada = item.ajuste.quantidade_ajustada if item.ajuste else None
        rows.append({
            'Codigo': item.produto.codigo,
            'Descricao': item.produto.descricao,
            'Corredor': item.produto.corredor or '',
            'Prateleira': item.produto.prateleira or '',
            'Unidade': item.produto.unidade,
            'Qtd Esperada': item.quantidade_esperada,
            'Qtd 1a Contagem': reg1.quantidade_contada if reg1 else '',
            'Responsavel 1a': reg1.usuario.nome if reg1 else '',
            'Qtd 2a Contagem': reg2.quantidade_contada if reg2 else '',
            'Responsavel 2a': reg2.usuario.nome if reg2 else '',
            'Qtd Ajustada': qtd_ajustada or '',
            'Justificativa': item.ajuste.justificativa if item.ajuste else '',
            'Status': item.status,
            'Divergencia': (qtd_ajustada or qtd_contada or 0) - item.quantidade_esperada if qtd_contada is not None else '',
        })

    df = pd.DataFrame(rows)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Contagem')
    output.seek(0)
    nome_arquivo = f"contagem_{ciclo.empresa.nome}_{ciclo.data_referencia.strftime('%Y%m%d')}.xlsx"
    return send_file(output, as_attachment=True, download_name=nome_arquivo,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ── ADMIN ─────────────────────────────────────────────────────────────────────

@app.route('/admin')
@login_required
@admin_required
def admin():
    empresas = Empresa.query.filter_by(ativo=True).all()
    usuarios = Usuario.query.filter_by(empresa_id=current_user.empresa_id).all()
    return render_template('admin.html', empresas=empresas, usuarios=usuarios)


@app.route('/admin/usuario/novo', methods=['POST'])
@login_required
@admin_required
def novo_usuario():
    nome = request.form.get('nome', '').strip()
    email = request.form.get('email', '').strip().lower()
    senha = request.form.get('senha', '')
    perfil = request.form.get('perfil', 'contador')

    if not nome or not email or not senha:
        flash('Preencha todos os campos.', 'warning')
        return redirect(url_for('admin'))

    if Usuario.query.filter_by(email=email).first():
        flash('Email já cadastrado.', 'warning')
        return redirect(url_for('admin'))

    usuario = Usuario(
        empresa_id=current_user.empresa_id,
        nome=nome,
        email=email,
        perfil=perfil
    )
    usuario.set_senha(senha)
    db.session.add(usuario)
    db.session.commit()
    flash(f'Usuário {nome} criado com sucesso.', 'success')
    return redirect(url_for('admin'))


@app.route('/admin/usuario/<int:uid>/toggle', methods=['POST'])
@login_required
@admin_required
def toggle_usuario(uid):
    usuario = Usuario.query.filter_by(id=uid, empresa_id=current_user.empresa_id).first_or_404()
    if usuario.id == current_user.id:
        flash('Você não pode desativar seu próprio usuário.', 'warning')
    else:
        usuario.ativo = not usuario.ativo
        db.session.commit()
        estado = 'ativado' if usuario.ativo else 'desativado'
        flash(f'Usuário {usuario.nome} {estado}.', 'success')
    return redirect(url_for('admin'))


# ── INIT DB ───────────────────────────────────────────────────────────────────

def criar_dados_iniciais():
    empresa = Empresa.query.first()
    if not empresa:
        empresa = Empresa(nome='Empresa Demo', cnpj='00.000.000/0001-00')
        db.session.add(empresa)
        db.session.flush()
        admin = Usuario(
            empresa_id=empresa.id,
            nome='Administrador',
            email='admin@demo.com',
            perfil='admin'
        )
        admin.set_senha('admin123')
        db.session.add(admin)
        db.session.commit()
        print('Dados iniciais criados. Login: admin@demo.com / admin123')


with app.app_context():
    db.create_all()
    criar_dados_iniciais()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
