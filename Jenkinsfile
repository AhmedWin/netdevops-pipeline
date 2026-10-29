// Same pipeline as .github/workflows/network.yml, on Jenkins.
//
// GitHub Actions is fine for a public repository, but a bank does not
// run its network changes on someone else's cloud runners. This is the
// same set of checks, in the same order, on a runner you control.
//
// Requires on the agent: docker, containerlab, python3-venv, and
// passwordless sudo for containerlab only. See README for setup.

pipeline {
    agent any

    options {
        // The lab has fixed container names, so two builds at once would
        // fight over the same topology.
        disableConcurrentBuilds()
        timeout(time: 25, unit: 'MINUTES')
    }

    environment {
        TOPOLOGY = 'lab/topology.clab.yml'
        VENV     = '.venv'
        PY       = './.venv/bin/python'
        // Tells the test suite a missing lab is a failure, not a reason
        // to skip - the whole point here is that the lab gets built.
        CI       = '1'
    }

    stages {

        stage('Install dependencies') {
            steps {
                sh '''
                    set -eu
                    python3 -m venv "$VENV"
                    "$PY" -m pip install --quiet --upgrade pip
                    "$PY" -m pip install --quiet -r requirements-dev.txt
                '''
            }
        }

        // 1. Is the design sound? Costs a tenth of a second, builds nothing.
        stage('Validate the intent') {
            steps {
                sh 'set -eu; "$PY" scripts/validate.py'
            }
        }

        // 2. Nobody hand-edited a generated file.
        stage('Configs match the source of truth') {
            steps {
                sh 'set -eu; "$PY" scripts/render.py --check'
            }
        }

        // 3. Build a disposable copy of the network.
        stage('Deploy the lab') {
            steps {
                sh '''
                    set -eu
                    sudo containerlab deploy -t "$TOPOLOGY" --reconfigure
                    sudo containerlab inspect -t "$TOPOLOGY"
                '''
            }
        }

        // 4. Test it. This is the gate.
        stage('Run network tests') {
            steps {
                sh 'set -eu; "$PY" -m pytest --junitxml=report.xml'
            }
        }
    }

    post {
        // A red build with no evidence gets ignored, and then disabled.
        failure {
            sh '''
                for r in r1 r2 r3; do
                    echo "===== $r ====="
                    docker exec "clab-netdevops-$r" vtysh -c "show bgp summary"   || true
                    docker exec "clab-netdevops-$r" vtysh -c "show ip route"      || true
                    docker exec "clab-netdevops-$r" vtysh -c "show interface brief" || true
                done
            '''
        }

        always {
            sh '''
                sudo containerlab destroy -t "$TOPOLOGY" --cleanup || true
                # containerlab writes its runtime directory as root; hand the
                # workspace back so Jenkins can clean it up next time.
                sudo chown -R "$(id -u):$(id -g)" . 2>/dev/null || true
            '''
            junit testResults: 'report.xml', allowEmptyResults: true
        }
    }
}
