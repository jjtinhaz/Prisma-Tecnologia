import os
import io
from dotenv import load_dotenv
load_dotenv()
from datetime import date, datetime
from flask import Flask, render_template, redirect, url_for, request, flash, send_file, jsonify
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from functools import wraps
import pandas as pd
from models import db, Empresa, Usuario, Produto, CicloContagem, ItemCiclo, RegistroContagem, AjusteDivergencia, ListaSeparacao, ItemSeparacao

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


PERFIL_LABELS = {
    'admin':      'Admin',
    'gerente':    'Gerente',
    'lider':      'Líder',
    'estoquista': 'Estoquista',
}


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.perfil != 'admin':
            flash('Acesso restrito a administradores.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


UNIDADES_INTEIRAS = {'UN', 'CX', 'PC', 'PÇ', 'PCT', 'PAR', 'PECA', 'PECAS', 'PEÇAS', 'UD'}


def validar_quantidade(quantidade, unidade):
    if quantidade < 0:
        return False, 'Quantidade não pode ser negativa.'
    if (unidade or '').upper().strip() in UNIDADES_INTEIRAS and quantidade != int(quantidade):
        return False, f'A unidade {unidade} não aceita decimais.'
    return True, None


def requer_perfil(*perfis):
    """Permite acesso a admin + qualquer perfil listado."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('login'))
            if current_user.perfil != 'admin' and current_user.perfil not in perfis:
                flash('Você não tem permissão para acessar esta página.', 'danger')
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return decorated
    return decorator


# ── AUTH ──────────────────────────────────────────────────────────────────────

@app.before_request
def verificar_troca_senha():
    if current_user.is_authenticated and getattr(current_user, 'deve_trocar_senha', False):
        if request.endpoint not in ('trocar_senha', 'logout', 'static'):
            return redirect(url_for('trocar_senha'))


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
            if usuario.deve_trocar_senha:
                return redirect(url_for('trocar_senha'))
            return redirect(url_for('dashboard'))
        flash('Email ou senha incorretos.', 'danger')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


@app.route('/trocar-senha', methods=['GET', 'POST'])
@login_required
def trocar_senha():
    if request.method == 'POST':
        nova = request.form.get('nova_senha', '').strip()
        confirmar = request.form.get('confirmar_senha', '').strip()
        if len(nova) < 6:
            flash('A senha deve ter pelo menos 6 caracteres.', 'warning')
            return redirect(url_for('trocar_senha'))
        if nova != confirmar:
            flash('As senhas não coincidem.', 'warning')
            return redirect(url_for('trocar_senha'))
        if nova == '123456':
            flash('Escolha uma senha diferente da senha padrão.', 'warning')
            return redirect(url_for('trocar_senha'))
        current_user.set_senha(nova)
        current_user.deve_trocar_senha = False
        db.session.commit()
        flash('Senha definida com sucesso. Bem-vindo!', 'success')
        return redirect(url_for('dashboard'))
    return render_template('trocar_senha.html')


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
@requer_perfil('gerente')
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
@requer_perfil('gerente')
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
@requer_perfil('estoquista', 'lider', 'gerente')
def contagem(ciclo_id):
    ciclo = CicloContagem.query.filter_by(
        id=ciclo_id, empresa_id=current_user.empresa_id
    ).first_or_404()
    filtro_corredor = request.args.get('corredor', '')
    query = ItemCiclo.query.filter(
        ItemCiclo.ciclo_id == ciclo_id,
        ItemCiclo.status == 'pendente'
    )
    if filtro_corredor:
        query = query.join(Produto).filter(Produto.corredor == filtro_corredor)
    itens = query.all()
    corredores = db.session.query(Produto.corredor).join(ItemCiclo).filter(
        ItemCiclo.ciclo_id == ciclo_id,
        ItemCiclo.status == 'pendente',
        Produto.corredor.isnot(None)
    ).distinct().all()
    corredores = [c[0] for c in corredores if c[0]]
    total_ciclo = ItemCiclo.query.filter_by(ciclo_id=ciclo_id).count()
    return render_template('contagem.html', ciclo=ciclo, itens=itens,
                           corredores=corredores, filtro_corredor=filtro_corredor,
                           total_ciclo=total_ciclo)


@app.route('/contagem/registrar', methods=['POST'])
@login_required
@requer_perfil('estoquista', 'lider', 'gerente')
def registrar_contagem():
    item_id = request.form.get('item_id', type=int)
    quantidade = request.form.get('quantidade', type=float)
    ciclo_id = request.form.get('ciclo_id', type=int)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    if item_id is None or quantidade is None:
        if is_ajax:
            return jsonify({'ok': False, 'msg': 'Dados inválidos.'}), 400
        flash('Dados inválidos.', 'danger')
        return redirect(url_for('contagem', ciclo_id=ciclo_id))

    item = ItemCiclo.query.join(CicloContagem).filter(
        ItemCiclo.id == item_id,
        CicloContagem.empresa_id == current_user.empresa_id
    ).first_or_404()

    if item.status != 'pendente':
        if is_ajax:
            return jsonify({'ok': False, 'msg': 'Item já foi contado.'}), 409
        flash('Este item já foi contado.', 'warning')
        return redirect(url_for('contagem', ciclo_id=ciclo_id))

    ok, msg = validar_quantidade(quantidade, item.produto.unidade)
    if not ok:
        if is_ajax:
            return jsonify({'ok': False, 'msg': msg}), 400
        flash(msg, 'warning')
        return redirect(url_for('contagem', ciclo_id=ciclo_id))

    registro = RegistroContagem(
        item_ciclo_id=item_id,
        usuario_id=current_user.id,
        quantidade_contada=quantidade,
        rodada=1
    )
    db.session.add(registro)

    item.status = 'divergente' if quantidade != item.quantidade_esperada else 'contado'
    db.session.commit()

    if is_ajax:
        return jsonify({'ok': True})
    return redirect(url_for('contagem', ciclo_id=ciclo_id))


@app.route('/ciclo/<int:ciclo_id>/gerar-segunda-contagem', methods=['POST'])
@login_required
@requer_perfil('lider', 'gerente')
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
@requer_perfil('lider', 'gerente')
def segunda_contagem(ciclo_id):
    ciclo = CicloContagem.query.filter_by(
        id=ciclo_id, empresa_id=current_user.empresa_id
    ).first_or_404()
    itens = ItemCiclo.query.filter_by(ciclo_id=ciclo_id, status='segunda_contagem').all()
    return render_template('segunda_contagem.html', ciclo=ciclo, itens=itens)


@app.route('/segunda-contagem/registrar', methods=['POST'])
@login_required
@requer_perfil('lider', 'gerente')
def registrar_segunda_contagem():
    item_id = request.form.get('item_id', type=int)
    quantidade = request.form.get('quantidade', type=float)
    ciclo_id = request.form.get('ciclo_id', type=int)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    if item_id is None or quantidade is None:
        if is_ajax:
            return jsonify({'ok': False, 'msg': 'Dados inválidos.'}), 400
        flash('Dados inválidos.', 'danger')
        return redirect(url_for('segunda_contagem', ciclo_id=ciclo_id))

    item = ItemCiclo.query.join(CicloContagem).filter(
        ItemCiclo.id == item_id,
        CicloContagem.empresa_id == current_user.empresa_id
    ).first_or_404()

    if item.status != 'segunda_contagem':
        if is_ajax:
            return jsonify({'ok': False, 'msg': 'Item já verificado.'}), 409
        flash('Este item já foi verificado.', 'warning')
        return redirect(url_for('segunda_contagem', ciclo_id=ciclo_id))

    ok, msg = validar_quantidade(quantidade, item.produto.unidade)
    if not ok:
        if is_ajax:
            return jsonify({'ok': False, 'msg': msg}), 400
        flash(msg, 'warning')
        return redirect(url_for('segunda_contagem', ciclo_id=ciclo_id))

    registro = RegistroContagem(
        item_ciclo_id=item_id,
        usuario_id=current_user.id,
        quantidade_contada=quantidade,
        rodada=2
    )
    db.session.add(registro)

    item.status = 'aprovado' if quantidade == item.quantidade_esperada else 'divergente'
    db.session.commit()

    if is_ajax:
        return jsonify({'ok': True})
    return redirect(url_for('segunda_contagem', ciclo_id=ciclo_id))


# ── DIVERGÊNCIAS ──────────────────────────────────────────────────────────────

@app.route('/divergencias/<int:ciclo_id>')
@login_required
@requer_perfil('gerente')
def divergencias(ciclo_id):
    ciclo = CicloContagem.query.filter_by(
        id=ciclo_id, empresa_id=current_user.empresa_id
    ).first_or_404()
    itens = ItemCiclo.query.filter_by(ciclo_id=ciclo_id, status='divergente').all()
    return render_template('divergencias.html', ciclo=ciclo, itens=itens)


@app.route('/divergencias/ajustar', methods=['POST'])
@login_required
@requer_perfil('gerente')
def ajustar_divergencia():
    item_id = request.form.get('item_id', type=int)
    quantidade_ajustada = request.form.get('quantidade_ajustada', type=float)
    justificativa = request.form.get('justificativa', '').strip()
    ciclo_id = request.form.get('ciclo_id', type=int)

    if not item_id or quantidade_ajustada is None or not justificativa:
        flash('Preencha todos os campos do ajuste.', 'warning')
        return redirect(url_for('divergencias', ciclo_id=ciclo_id))

    if quantidade_ajustada < 0:
        flash('Quantidade ajustada não pode ser negativa.', 'warning')
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


@app.route('/ciclo/<int:ciclo_id>/deletar', methods=['POST'])
@login_required
@admin_required
def deletar_ciclo(ciclo_id):
    ciclo = CicloContagem.query.filter_by(
        id=ciclo_id, empresa_id=current_user.empresa_id
    ).first_or_404()
    if ciclo.status == 'fechado':
        flash('Ciclos fechados não podem ser deletados.', 'warning')
        return redirect(url_for('dashboard'))
    data = ciclo.data_referencia.strftime('%d/%m/%Y')
    db.session.delete(ciclo)
    db.session.commit()
    flash(f'Ciclo de {data} deletado.', 'success')
    return redirect(url_for('dashboard'))


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
        def fmt_dt(dt):
            return dt.strftime('%d/%m/%Y %H:%M') if dt else ''

        rows.append({
            'Codigo': item.produto.codigo,
            'Descricao': item.produto.descricao,
            'Corredor': item.produto.corredor or '',
            'Prateleira': item.produto.prateleira or '',
            'Unidade': item.produto.unidade,
            'Qtd Esperada': item.quantidade_esperada,
            'Qtd 1a Contagem': reg1.quantidade_contada if reg1 else '',
            'Responsavel 1a': reg1.usuario.nome if reg1 else '',
            'Data 1a Contagem': fmt_dt(reg1.data_contagem if reg1 else None),
            'Qtd 2a Contagem': reg2.quantidade_contada if reg2 else '',
            'Responsavel 2a': reg2.usuario.nome if reg2 else '',
            'Data 2a Contagem': fmt_dt(reg2.data_contagem if reg2 else None),
            'Qtd Ajustada': qtd_ajustada or '',
            'Responsavel Ajuste': item.ajuste.usuario.nome if item.ajuste else '',
            'Data Ajuste': fmt_dt(item.ajuste.data_ajuste if item.ajuste else None),
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
    perfil = request.form.get('perfil', 'estoquista')
    if perfil not in PERFIL_LABELS:
        perfil = 'estoquista'

    if not nome or not email:
        flash('Preencha todos os campos.', 'warning')
        return redirect(url_for('admin'))

    if Usuario.query.filter_by(email=email).first():
        flash('Email já cadastrado.', 'warning')
        return redirect(url_for('admin'))

    usuario = Usuario(
        empresa_id=current_user.empresa_id,
        nome=nome,
        email=email,
        perfil=perfil,
        deve_trocar_senha=True
    )
    usuario.set_senha('123456')
    db.session.add(usuario)
    db.session.commit()
    flash(f'Usuário {nome} criado. Senha inicial: 123456.', 'success')
    return redirect(url_for('admin'))


@app.route('/admin/usuario/<int:uid>/editar', methods=['GET', 'POST'])
@login_required
@admin_required
def editar_usuario(uid):
    usuario = Usuario.query.filter_by(id=uid, empresa_id=current_user.empresa_id).first_or_404()
    if request.method == 'POST':
        nome = request.form.get('nome', '').strip()
        email = request.form.get('email', '').strip().lower()
        perfil = request.form.get('perfil', usuario.perfil)
        if not nome or not email:
            flash('Preencha todos os campos.', 'warning')
            return redirect(url_for('editar_usuario', uid=uid))
        if perfil not in PERFIL_LABELS:
            perfil = usuario.perfil
        conflito = Usuario.query.filter(Usuario.email == email, Usuario.id != uid).first()
        if conflito:
            flash('Este email já está em uso.', 'warning')
            return redirect(url_for('editar_usuario', uid=uid))
        usuario.nome = nome
        usuario.email = email
        usuario.perfil = perfil
        db.session.commit()
        flash(f'Usuário {nome} atualizado.', 'success')
        return redirect(url_for('admin'))
    return render_template('editar_usuario.html', usuario=usuario)


@app.route('/admin/usuario/<int:uid>/resetar-senha', methods=['POST'])
@login_required
@admin_required
def resetar_senha(uid):
    usuario = Usuario.query.filter_by(id=uid, empresa_id=current_user.empresa_id).first_or_404()
    usuario.set_senha('123456')
    usuario.deve_trocar_senha = True
    db.session.commit()
    flash(f'Senha de {usuario.nome} resetada. Próximo acesso: 123456.', 'success')
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


# ── LISTA DE SEPARAÇÃO ────────────────────────────────────────────────────────

@app.route('/separacao')
@login_required
def separacao_lista():
    listas = ListaSeparacao.query.filter_by(empresa_id=current_user.empresa_id)\
        .order_by(ListaSeparacao.data_criacao.desc()).all()
    return render_template('separacao_lista.html', listas=listas)


@app.route('/separacao/nova', methods=['GET', 'POST'])
@login_required
@requer_perfil('gerente', 'lider')
def separacao_nova():
    if request.method == 'POST':
        arquivo = request.files.get('arquivo')
        descricao = request.form.get('descricao', '').strip()
        if not arquivo:
            flash('Selecione o arquivo Excel.', 'warning')
            return redirect(url_for('separacao_nova'))
        try:
            df = pd.read_excel(arquivo, dtype={'Codigo': str})
            if 'Codigo' not in df.columns or 'Quantidade' not in df.columns:
                flash('O arquivo deve ter as colunas: Codigo, Quantidade', 'danger')
                return redirect(url_for('separacao_nova'))

            lista = ListaSeparacao(
                empresa_id=current_user.empresa_id,
                descricao=descricao or None,
                criado_por_id=current_user.id,
                status='aberta'
            )
            db.session.add(lista)
            db.session.flush()

            nao_encontrados = []
            total = 0
            for _, row in df.iterrows():
                codigo = str(row['Codigo']).strip()
                try:
                    quantidade = float(row['Quantidade'])
                except (ValueError, TypeError):
                    continue
                if quantidade <= 0:
                    continue
                produto = Produto.query.filter_by(
                    empresa_id=current_user.empresa_id, codigo=codigo, ativo=True
                ).first()
                if not produto:
                    nao_encontrados.append(codigo)
                    continue
                item = ItemSeparacao(
                    lista_id=lista.id,
                    produto_id=produto.id,
                    quantidade_solicitada=quantidade
                )
                db.session.add(item)
                total += 1

            if total == 0:
                db.session.rollback()
                flash('Nenhum produto válido encontrado no arquivo.', 'danger')
                return redirect(url_for('separacao_nova'))

            db.session.commit()
            if nao_encontrados:
                flash(f'Lista criada com {total} item(ns). Códigos não encontrados: {", ".join(nao_encontrados[:10])}.', 'warning')
            else:
                flash(f'Lista de separação criada com {total} item(ns).', 'success')
            return redirect(url_for('separacao_detalhe', lista_id=lista.id))

        except Exception as e:
            db.session.rollback()
            flash(f'Erro ao processar arquivo: {str(e)}', 'danger')

    return render_template('separacao_nova.html')


@app.route('/separacao/<int:lista_id>')
@login_required
def separacao_detalhe(lista_id):
    lista = ListaSeparacao.query.filter_by(
        id=lista_id, empresa_id=current_user.empresa_id
    ).first_or_404()
    filtro_corredor = request.args.get('corredor', '')
    itens_pendentes = [i for i in lista.itens if i.status == 'pendente']
    if filtro_corredor:
        itens_pendentes = [i for i in itens_pendentes if i.produto.corredor == filtro_corredor]
    corredores = sorted({i.produto.corredor for i in lista.itens if i.produto.corredor})
    total = len(lista.itens)
    concluidos = sum(1 for i in lista.itens if i.status != 'pendente')
    return render_template('separacao.html', lista=lista, itens=itens_pendentes,
                           corredores=corredores, filtro_corredor=filtro_corredor,
                           total=total, concluidos=concluidos)


@app.route('/separacao/registrar', methods=['POST'])
@login_required
def registrar_separacao():
    item_id = request.form.get('item_id', type=int)
    quantidade_separada = request.form.get('quantidade_separada', type=float)
    status_item = request.form.get('status_item', 'separado')
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    if item_id is None or status_item not in ('separado', 'parcial', 'nao_encontrado'):
        if is_ajax:
            return jsonify({'ok': False, 'msg': 'Dados inválidos.'}), 400
        flash('Dados inválidos.', 'danger')
        return redirect(url_for('separacao_lista'))

    item = ItemSeparacao.query.join(ListaSeparacao).filter(
        ItemSeparacao.id == item_id,
        ListaSeparacao.empresa_id == current_user.empresa_id
    ).first_or_404()

    if item.status != 'pendente':
        if is_ajax:
            return jsonify({'ok': False, 'msg': 'Item já separado.'}), 409
        flash('Item já separado.', 'warning')
        return redirect(url_for('separacao_detalhe', lista_id=item.lista_id))

    if status_item in ('separado', 'parcial') and (quantidade_separada is None or quantidade_separada < 0):
        if is_ajax:
            return jsonify({'ok': False, 'msg': 'Informe a quantidade separada.'}), 400
        flash('Informe a quantidade separada.', 'warning')
        return redirect(url_for('separacao_detalhe', lista_id=item.lista_id))

    item.status = status_item
    item.quantidade_separada = quantidade_separada if status_item != 'nao_encontrado' else 0
    item.separado_por_id = current_user.id
    item.data_separacao = datetime.utcnow()

    if all(i.status != 'pendente' for i in item.lista.itens if i.id != item_id) and item.status != 'pendente':
        if item.lista.status == 'aberta':
            item.lista.status = 'em_separacao'
        todos_concluidos = all(i.status != 'pendente' for i in item.lista.itens)
        if todos_concluidos:
            item.lista.status = 'concluida'

    db.session.commit()

    if is_ajax:
        return jsonify({'ok': True})
    return redirect(url_for('separacao_detalhe', lista_id=item.lista_id))


@app.route('/separacao/<int:lista_id>/concluir', methods=['POST'])
@login_required
@requer_perfil('gerente', 'lider')
def concluir_separacao(lista_id):
    lista = ListaSeparacao.query.filter_by(
        id=lista_id, empresa_id=current_user.empresa_id
    ).first_or_404()
    lista.status = 'concluida'
    db.session.commit()
    flash('Lista de separação concluída.', 'success')
    return redirect(url_for('separacao_lista'))


@app.route('/separacao/<int:lista_id>/cancelar', methods=['POST'])
@login_required
@requer_perfil('gerente', 'lider')
def cancelar_separacao(lista_id):
    lista = ListaSeparacao.query.filter_by(
        id=lista_id, empresa_id=current_user.empresa_id
    ).first_or_404()
    lista.status = 'cancelada'
    db.session.commit()
    flash('Lista de separação cancelada.', 'warning')
    return redirect(url_for('separacao_lista'))


@app.route('/separacao/<int:lista_id>/exportar')
@login_required
def exportar_separacao(lista_id):
    lista = ListaSeparacao.query.filter_by(
        id=lista_id, empresa_id=current_user.empresa_id
    ).first_or_404()
    rows = []
    for item in lista.itens:
        rows.append({
            'Codigo': item.produto.codigo,
            'Descricao': item.produto.descricao,
            'Corredor': item.produto.corredor or '',
            'Prateleira': item.produto.prateleira or '',
            'Unidade': item.produto.unidade,
            'Qtd Solicitada': item.quantidade_solicitada,
            'Qtd Separada': item.quantidade_separada if item.quantidade_separada is not None else '',
            'Status': item.status,
            'Separado Por': item.separado_por.nome if item.separado_por else '',
            'Data Separacao': item.data_separacao.strftime('%d/%m/%Y %H:%M') if item.data_separacao else '',
        })
    df = pd.DataFrame(rows)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Separacao')
    output.seek(0)
    nome = f"separacao_{lista_id}_{datetime.utcnow().strftime('%Y%m%d')}.xlsx"
    return send_file(output, as_attachment=True, download_name=nome,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ── PAINEL DE ESTOQUE ─────────────────────────────────────────────────────────

def _calcular_estoque_atual(empresa_id):
    """Retorna dict {produto_id: qtd_atual} baseado no último ciclo fechado."""
    ciclos_fechados = CicloContagem.query.filter_by(
        empresa_id=empresa_id, status='fechado'
    ).order_by(CicloContagem.data_referencia.desc()).all()

    estoque = {}
    for ciclo in ciclos_fechados:
        for item in ciclo.itens:
            if item.produto_id in estoque:
                continue
            if item.ajuste:
                qtd = item.ajuste.quantidade_ajustada
            else:
                reg = next((r for r in sorted(item.registros, key=lambda r: r.rodada, reverse=True)), None)
                if reg:
                    qtd = reg.quantidade_contada
                else:
                    qtd = item.quantidade_esperada
            estoque[item.produto_id] = qtd
    return estoque


@app.route('/painel-estoque')
@login_required
@requer_perfil('gerente', 'lider')
def painel_estoque():
    produtos = Produto.query.filter_by(empresa_id=current_user.empresa_id, ativo=True)\
        .order_by(Produto.corredor, Produto.codigo).all()
    estoque_atual = _calcular_estoque_atual(current_user.empresa_id)

    ultimo_ciclo = CicloContagem.query.filter_by(
        empresa_id=current_user.empresa_id, status='fechado'
    ).order_by(CicloContagem.data_referencia.desc()).first()

    total_skus = len(produtos)
    com_estoque = sum(1 for p in produtos if estoque_atual.get(p.id, None) and estoque_atual.get(p.id) > 0)
    zerados = sum(1 for p in produtos if p.id in estoque_atual and estoque_atual[p.id] == 0)
    sem_dados = sum(1 for p in produtos if p.id not in estoque_atual)

    filtro_corredor = request.args.get('corredor', '')
    filtro_abc = request.args.get('abc', '')

    itens_painel = []
    for p in produtos:
        if filtro_corredor and p.corredor != filtro_corredor:
            continue
        if filtro_abc and p.curva_abc != filtro_abc:
            continue
        itens_painel.append({
            'produto': p,
            'qtd_atual': estoque_atual.get(p.id),
        })

    corredores = sorted({p.corredor for p in produtos if p.corredor})

    return render_template('painel_estoque.html',
                           itens=itens_painel,
                           corredores=corredores,
                           filtro_corredor=filtro_corredor,
                           filtro_abc=filtro_abc,
                           total_skus=total_skus,
                           com_estoque=com_estoque,
                           zerados=zerados,
                           sem_dados=sem_dados,
                           ultimo_ciclo=ultimo_ciclo)


# ── SUGESTÃO DE COMPRA ────────────────────────────────────────────────────────

@app.route('/sugestao-compra')
@login_required
@requer_perfil('gerente', 'lider')
def sugestao_compra():
    produtos = Produto.query.filter_by(empresa_id=current_user.empresa_id, ativo=True)\
        .order_by(Produto.curva_abc, Produto.codigo).all()
    estoque_atual = _calcular_estoque_atual(current_user.empresa_id)

    filtro_abc = request.args.get('abc', '')
    apenas_sugestao = request.args.get('apenas_sugestao', '0') == '1'

    itens = []
    for p in produtos:
        if filtro_abc and p.curva_abc != filtro_abc:
            continue
        qtd_atual = estoque_atual.get(p.id)
        sugestao = None
        if p.estoque_minimo is not None and qtd_atual is not None:
            diff = p.estoque_minimo - qtd_atual
            sugestao = max(diff, 0)
        elif p.estoque_minimo is not None and qtd_atual is None:
            sugestao = p.estoque_minimo
        elif p.curva_abc == 'A' and qtd_atual == 0:
            sugestao = 0

        if apenas_sugestao and (sugestao is None or sugestao == 0):
            continue

        itens.append({
            'produto': p,
            'qtd_atual': qtd_atual,
            'sugestao': sugestao,
        })

    return render_template('sugestao_compra.html',
                           itens=itens,
                           filtro_abc=filtro_abc,
                           apenas_sugestao=apenas_sugestao)


@app.route('/sugestao-compra/calcular-abc', methods=['POST'])
@login_required
@requer_perfil('gerente')
def calcular_abc():
    data_limite = date.today().replace(year=date.today().year - 1)

    ciclos = CicloContagem.query.filter(
        CicloContagem.empresa_id == current_user.empresa_id,
        CicloContagem.status == 'fechado',
        CicloContagem.data_referencia >= data_limite
    ).order_by(CicloContagem.data_referencia.asc()).all()

    if len(ciclos) < 2:
        flash('São necessários ao menos 2 ciclos fechados nos últimos 12 meses para calcular a Curva ABC.', 'warning')
        return redirect(url_for('sugestao_compra'))

    estoque_por_ciclo = {}
    for ciclo in ciclos:
        snap = {}
        for item in ciclo.itens:
            if item.ajuste:
                qtd = item.ajuste.quantidade_ajustada
            else:
                reg = next((r for r in sorted(item.registros, key=lambda r: r.rodada, reverse=True)), None)
                qtd = reg.quantidade_contada if reg else item.quantidade_esperada
            snap[item.produto_id] = qtd
        estoque_por_ciclo[ciclo.id] = snap

    consumo = {}
    ciclo_ids = [c.id for c in ciclos]
    for i in range(len(ciclo_ids) - 1):
        anterior = estoque_por_ciclo[ciclo_ids[i]]
        posterior = estoque_por_ciclo[ciclo_ids[i + 1]]
        for pid, qtd_ant in anterior.items():
            qtd_pos = posterior.get(pid, qtd_ant)
            delta = qtd_ant - qtd_pos
            if delta > 0:
                consumo[pid] = consumo.get(pid, 0) + delta

    if not consumo:
        flash('Não há variação de estoque entre os ciclos para calcular a Curva ABC.', 'warning')
        return redirect(url_for('sugestao_compra'))

    total_consumo = sum(consumo.values())
    ordenados = sorted(consumo.items(), key=lambda x: x[1], reverse=True)
    acumulado = 0
    classificacao = {}
    for pid, cons in ordenados:
        acumulado += cons
        pct = acumulado / total_consumo
        if pct <= 0.80:
            classificacao[pid] = 'A'
        elif pct <= 0.95:
            classificacao[pid] = 'B'
        else:
            classificacao[pid] = 'C'

    produtos = Produto.query.filter_by(empresa_id=current_user.empresa_id, ativo=True).all()
    for p in produtos:
        p.curva_abc = classificacao.get(p.id, 'C' if p.id in consumo else None)
    db.session.commit()

    flash(f'Curva ABC recalculada para {len(classificacao)} produto(s).', 'success')
    return redirect(url_for('sugestao_compra'))


@app.route('/produto/<int:produto_id>/estoque-minimo', methods=['POST'])
@login_required
@requer_perfil('gerente')
def atualizar_estoque_minimo(produto_id):
    produto = Produto.query.filter_by(
        id=produto_id, empresa_id=current_user.empresa_id, ativo=True
    ).first_or_404()
    valor = request.form.get('estoque_minimo', type=float)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if valor is not None and valor >= 0:
        produto.estoque_minimo = valor
        db.session.commit()
        if is_ajax:
            return jsonify({'ok': True, 'valor': valor})
    else:
        if is_ajax:
            return jsonify({'ok': False, 'msg': 'Valor inválido.'}), 400
    return redirect(url_for('sugestao_compra'))


@app.route('/sugestao-compra/exportar')
@login_required
@requer_perfil('gerente', 'lider')
def exportar_sugestao():
    produtos = Produto.query.filter_by(empresa_id=current_user.empresa_id, ativo=True)\
        .order_by(Produto.curva_abc, Produto.codigo).all()
    estoque_atual = _calcular_estoque_atual(current_user.empresa_id)
    rows = []
    for p in produtos:
        qtd_atual = estoque_atual.get(p.id)
        sugestao = None
        if p.estoque_minimo is not None:
            qtd = qtd_atual if qtd_atual is not None else 0
            sugestao = max(p.estoque_minimo - qtd, 0)
        rows.append({
            'Codigo': p.codigo,
            'Descricao': p.descricao,
            'Corredor': p.corredor or '',
            'Unidade': p.unidade,
            'Curva ABC': p.curva_abc or '',
            'Estoque Atual': qtd_atual if qtd_atual is not None else '',
            'Estoque Minimo': p.estoque_minimo if p.estoque_minimo is not None else '',
            'Sugestao Compra': sugestao if sugestao is not None else '',
        })
    df = pd.DataFrame(rows)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Sugestao')
    output.seek(0)
    nome = f"sugestao_compra_{datetime.utcnow().strftime('%Y%m%d')}.xlsx"
    return send_file(output, as_attachment=True, download_name=nome,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


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
            perfil='admin',
            deve_trocar_senha=False
        )
        admin.set_senha('admin123')
        db.session.add(admin)
        db.session.commit()
        print('Dados iniciais criados. Login: admin@demo.com / admin123')


@app.context_processor
def inject_globals():
    return {'perfil_labels': PERFIL_LABELS}


with app.app_context():
    db.create_all()
    try:
        db.session.execute(db.text(
            'ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS deve_trocar_senha BOOLEAN NOT NULL DEFAULT FALSE'
        ))
        db.session.execute(db.text(
            'ALTER TABLE produtos ADD COLUMN IF NOT EXISTS estoque_minimo FLOAT'
        ))
        db.session.execute(db.text(
            'ALTER TABLE produtos ADD COLUMN IF NOT EXISTS curva_abc VARCHAR(1)'
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()
    criar_dados_iniciais()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
