"""Initial PostgreSQL schema for Vocalis.

Revision ID: 0001_postgres
Revises:
"""
from alembic import op
import sqlalchemy as sa


revision = '0001_postgres'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'tenants',
        sa.Column('id',sa.Text(),primary_key=True),
        sa.Column('name',sa.Text(),nullable=False),
        sa.Column('api_key_hash',sa.Text(),nullable=False,unique=True),
        sa.Column('config',sa.Text(),nullable=False,server_default='{}'),
    )
    op.create_table(
        'calls',
        sa.Column('id',sa.Text(),primary_key=True),
        sa.Column('tenant_id',sa.Text(),sa.ForeignKey('tenants.id'),nullable=False),
        sa.Column('caller',sa.Text()),
        sa.Column('channel',sa.Text(),nullable=False),
        sa.Column('status',sa.Text(),nullable=False),
        sa.Column('state',sa.Text(),nullable=False,server_default='{}'),
        sa.Column('started_at',sa.Text(),nullable=False),
        sa.Column('ended_at',sa.Text()),
        sa.UniqueConstraint('tenant_id','id',name='calls_tenant_id_key'),
    )
    op.create_index('calls_tenant','calls',['tenant_id','started_at'])
    op.create_table(
        'events',
        sa.Column('id',sa.BigInteger(),sa.Identity(),primary_key=True),
        sa.Column('tenant_id',sa.Text(),sa.ForeignKey('tenants.id'),nullable=False),
        sa.Column('call_id',sa.Text(),nullable=False),
        sa.Column('kind',sa.Text(),nullable=False),
        sa.Column('payload',sa.Text(),nullable=False),
        sa.Column('at',sa.Text(),nullable=False),
        sa.ForeignKeyConstraint(['tenant_id','call_id'],['calls.tenant_id','calls.id'],name='events_tenant_call_fkey'),
    )
    op.create_index('events_call','events',['tenant_id','call_id','id'])
    op.create_table(
        'documents',
        sa.Column('id',sa.Text(),primary_key=True),
        sa.Column('tenant_id',sa.Text(),sa.ForeignKey('tenants.id'),nullable=False),
        sa.Column('title',sa.Text(),nullable=False),
        sa.Column('content',sa.Text(),nullable=False),
        sa.Column('created_at',sa.Text(),nullable=False),
    )
    op.create_index('documents_tenant','documents',['tenant_id'])
    op.create_table(
        'appointments',
        sa.Column('id',sa.Text(),primary_key=True),
        sa.Column('tenant_id',sa.Text(),sa.ForeignKey('tenants.id'),nullable=False),
        sa.Column('external_id',sa.Text()),
        sa.Column('name',sa.Text(),nullable=False),
        sa.Column('phone',sa.Text(),nullable=False),
        sa.Column('service',sa.Text(),nullable=False),
        sa.Column('start_at',sa.Text(),nullable=False),
        sa.Column('end_at',sa.Text(),nullable=False),
        sa.Column('status',sa.Text(),nullable=False),
        sa.Column('request_key',sa.Text()),
        sa.Column('request_hash',sa.Text()),
        sa.Column('reservation_at',sa.Text()),
        sa.UniqueConstraint('tenant_id','id',name='appointments_tenant_id_key'),
    )
    op.create_index('appointments_tenant','appointments',['tenant_id','start_at'])
    op.create_index('appointments_active_slot','appointments',['tenant_id','start_at'],
                    unique=True,postgresql_where=sa.text("status='confirmed'"))
    op.create_index('appointments_request_key','appointments',['tenant_id','request_key'],
                    unique=True,postgresql_where=sa.text('request_key IS NOT NULL'))
    op.create_table(
        'leads',
        sa.Column('id',sa.Text(),primary_key=True),
        sa.Column('tenant_id',sa.Text(),sa.ForeignKey('tenants.id'),nullable=False),
        sa.Column('name',sa.Text(),nullable=False),
        sa.Column('phone',sa.Text(),nullable=False),
        sa.Column('email',sa.Text()),
        sa.Column('status',sa.Text(),nullable=False),
        sa.Column('external_id',sa.Text()),
        sa.UniqueConstraint('tenant_id','id',name='leads_tenant_id_key'),
        sa.UniqueConstraint('tenant_id','phone',name='leads_tenant_phone_key'),
    )
    op.create_table(
        'lead_sync_jobs',
        sa.Column('tenant_id',sa.Text(),sa.ForeignKey('tenants.id'),nullable=False),
        sa.Column('lead_id',sa.Text(),nullable=False),
        sa.Column('status',sa.Text(),nullable=False,server_default='pending'),
        sa.Column('attempts',sa.Integer(),nullable=False,server_default='0'),
        sa.Column('last_error',sa.Text()),
        sa.Column('updated_at',sa.Text(),nullable=False),
        sa.Column('lease_token',sa.Text()),
        sa.Column('lease_until',sa.Text()),
        sa.Column('available_at',sa.Text(),nullable=False,server_default=''),
        sa.Column('payload_hash',sa.Text(),nullable=False,server_default=''),
        sa.ForeignKeyConstraint(['tenant_id','lead_id'],['leads.tenant_id','leads.id'],name='lead_sync_jobs_tenant_lead_fkey'),
        sa.PrimaryKeyConstraint('tenant_id','lead_id',name='lead_sync_jobs_pkey'),
    )
    op.create_index('lead_sync_jobs_pending','lead_sync_jobs',['status','available_at','updated_at'])
    op.create_table(
        'idempotency',
        sa.Column('tenant_id',sa.Text(),sa.ForeignKey('tenants.id'),nullable=False),
        sa.Column('key',sa.Text(),nullable=False),
        sa.Column('result',sa.Text(),nullable=False),
        sa.Column('request_hash',sa.Text()),
        sa.PrimaryKeyConstraint('tenant_id','key',name='idempotency_pkey'),
    )
    op.create_table(
        'webhooks',
        sa.Column('provider',sa.Text(),nullable=False),
        sa.Column('event_id',sa.Text(),nullable=False),
        sa.Column('tenant_id',sa.Text(),sa.ForeignKey('tenants.id'),nullable=False),
        sa.Column('received_at',sa.Text(),nullable=False),
        sa.PrimaryKeyConstraint('provider','event_id',name='webhooks_pkey'),
    )
    op.create_table(
        'webhook_turns',
        sa.Column('tenant_id',sa.Text(),sa.ForeignKey('tenants.id'),nullable=False),
        sa.Column('key',sa.Text(),nullable=False),
        sa.Column('request_hash',sa.Text(),nullable=False),
        sa.Column('status',sa.Text(),nullable=False),
        sa.Column('token',sa.Text(),nullable=False),
        sa.Column('lease_until',sa.Text(),nullable=False),
        sa.Column('response',sa.Text()),
        sa.PrimaryKeyConstraint('tenant_id','key',name='webhook_turns_pkey'),
    )
    op.create_table(
        'appointment_operations',
        sa.Column('tenant_id',sa.Text(),sa.ForeignKey('tenants.id'),nullable=False),
        sa.Column('key',sa.Text(),nullable=False),
        sa.Column('appointment_id',sa.Text(),nullable=False),
        sa.Column('kind',sa.Text(),nullable=False),
        sa.Column('request_hash',sa.Text(),nullable=False),
        sa.Column('status',sa.Text(),nullable=False),
        sa.Column('token',sa.Text(),nullable=False),
        sa.Column('lease_until',sa.Text(),nullable=False),
        sa.Column('new_start',sa.Text()),
        sa.Column('new_end',sa.Text()),
        sa.Column('result',sa.Text()),
        sa.ForeignKeyConstraint(['tenant_id','appointment_id'],['appointments.tenant_id','appointments.id'],name='appointment_operations_tenant_appointment_fkey'),
        sa.PrimaryKeyConstraint('tenant_id','key',name='appointment_operations_pkey'),
    )
    op.create_index('appointment_operations_active','appointment_operations',
                    ['tenant_id','appointment_id'],unique=True,
                    postgresql_where=sa.text("status='pending'"))


def downgrade():
    op.drop_index('appointment_operations_active',table_name='appointment_operations')
    op.drop_table('appointment_operations')
    op.drop_table('webhook_turns')
    op.drop_table('webhooks')
    op.drop_table('idempotency')
    op.drop_index('lead_sync_jobs_pending',table_name='lead_sync_jobs')
    op.drop_table('lead_sync_jobs')
    op.drop_table('leads')
    op.drop_index('appointments_request_key',table_name='appointments')
    op.drop_index('appointments_active_slot',table_name='appointments')
    op.drop_index('appointments_tenant',table_name='appointments')
    op.drop_table('appointments')
    op.drop_index('documents_tenant',table_name='documents')
    op.drop_table('documents')
    op.drop_index('events_call',table_name='events')
    op.drop_table('events')
    op.drop_index('calls_tenant',table_name='calls')
    op.drop_table('calls')
    op.drop_table('tenants')
